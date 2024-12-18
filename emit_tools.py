"""
This Module has the functions related to working with an EMIT dataset. This includes doing things
like opening and flattening the data to work in xarray, orthorectification, and extracting point and area samples.

Author: Erik Bolch, ebolch@contractor.usgs.gov 

Last Updated: 05/09/2024

TO DO: 
- Rework masking functions to be more flexible
- Update format to match AppEEARS outputs


NOTE:
This was reworked a bit from the last updated (see above) by myself (Brent). Mostly a lot of front-end stuff to make it loook nice and 
work well for daata exploration.

Most of this code is not mine!



"""

# Packages used
import netCDF4 as nc
import os
from io import BytesIO
from spectral.io import envi
from osgeo import gdal
import numpy as np
import math
from skimage import io
import pandas as pd
import geopandas as gpd
import xarray as xr
import rasterio as rio
import rioxarray as rxr
import s3fs
from rioxarray.merge import merge_arrays
from fsspec.implementations.http import HTTPFile
from satsearch import Search
import requests
import ipywidgets as widgets
from ipywidgets import Button, VBox
from IPython.display import display
import matplotlib.pyplot as plt
from PIL import Image
import random


def get_image_selector(lat, lon):
    """
    Fetch available images for given latitude and longitude, and provide
    an interactive widget to select a date and view corresponding URLs.

    Parameters:
    lat (float): Latitude for image search
    lon (float): Longitude for image search
    """

    # Fetch available images
    available_images = get_images(lat, lon)

    if 'error' in available_images:  # Handle case if no valid images found
        print(available_images['error'])
        return None
    
    # Create a dropdown menu with available images
    date_selector = widgets.Dropdown(
        options=[(img['start_datetime'], img) for img in available_images],
        description='Select Date:',
        style={'description_width': 'initial'}
    )
    
    # Output widget to display the image
    output = widgets.Output()

    # URL container widgets that hold the selected URLs
    png_url_widget = widgets.Text(
        value='', 
        description='PNG URL:', 
        disabled=True,
        layout=widgets.Layout(width='100%')
    )
    
    rfl_url_widget = widgets.Text(
        value='', 
        description='RFL URL:', 
        disabled=True,
        layout=widgets.Layout(width='100%')
    )
    
    # Function to display URLs and plot image
    def on_date_change(change):
        output.clear_output()  # Clear previous output when the date changes
        selected_item = change['new']
        if selected_item:
            png_urls = selected_item['png_urls']
            rfl_urls = selected_item['rfl_urls']
            
            # Display the URLs in their respective widgets
            png_url_widget.value = png_urls[0] if png_urls else "No PNG found"
            rfl_url_widget.value = rfl_urls[0] if rfl_urls else "No RFL found"
            
            # Plot the PNG image immediately after selection
            with output:
                if png_urls:
                    selected_png_url = png_urls[0]
                    response = requests.get(selected_png_url)
                    if response.status_code == 200:
                        img = Image.open(BytesIO(response.content))
                        display(img)  # Display the new image in the widget
                else:
                    print("No PNG URL found.")
    
    # Attach the function to the dropdown widget
    date_selector.observe(on_date_change, names='value')

    # Display the dropdown widget, output area, and URL text fields
    display(date_selector, output, png_url_widget, rfl_url_widget)
    
    return png_url_widget, rfl_url_widget







def get_images(lat, lon):
    try:
        # Define a bounding box around the point
        bbox = [lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1]
        
        # Define the base CMR-STAC search URL for EMIT
        CMR_STAC_URL = 'https://cmr.earthdata.nasa.gov/stac/LPCLOUD/?page=2'
        
        # Perform the search
        search = Search(
            url=CMR_STAC_URL,
            bbox=bbox,
            collections=["EMITL2ARFL_001"],
            limit=5000
        )
        
        # Fetch items from the search
        item_collection = search.items()
        
        # Extract available images
        available_images = []
        for item in item_collection._items:
            properties = item._data.get('properties', {})
            start_datetime = properties.get('start_datetime', None)
            assets = item._data.get('assets', {})

            # List of desired asset substrings (RFL, PNG)
            #desired_assets = ['_RFL_', '_png_']
            filtered_asset_links = {'png': [], 'rfl': []}  # Dictionary to separate URLs

            # Filter assets based on desired substrings
            for asset in assets.values():
                asset_url = asset.get('href', None)
                if asset_url:
                    asset_name = asset_url.split('/')[-1]
                    if 'png' in asset_name:
                        filtered_asset_links['png'].append(asset_url)
                    elif 'EMIT_L2A_RFL' in asset_name:
                        filtered_asset_links['rfl'].append(asset_url)

            if start_datetime and (filtered_asset_links['png'] or filtered_asset_links['rfl']):
                available_images.append({
                    'start_datetime': start_datetime,
                    'png_urls': filtered_asset_links['png'],  # PNG URLs
                    'rfl_urls': filtered_asset_links['rfl']   # RFL URLs
                })

        if not available_images:
            return {'error': 'No valid images found for the given coordinates'}
        
        return available_images  # Return available images with both PNG and RFL URLs
    except Exception as e:
        print(f"Error: {e}")
        return {'error': 'An error occurred while fetching images'}















def emit_xarray(filepath, ortho=False, qmask=None, unpacked_bmask=None):
    """
    This function utilizes other functions in this module to streamline opening an EMIT dataset as an xarray.Dataset.

    Parameters:
    filepath: a filepath to an EMIT netCDF file
    ortho: True or False, whether to orthorectify the dataset or leave in crosstrack/downtrack coordinates.
    qmask: a numpy array output from the quality_mask function used to mask pixels based on quality flags selected in that function. Any non-orthorectified array with the proper crosstrack and downtrack dimensions can also be used.
    unpacked_bmask: a numpy array from  the band_mask function that can be used to mask band-specific pixels that have been interpolated.

    Returns:
    out_xr: an xarray.Dataset constructed based on the parameters provided.

    """

    # Check file type
    if isinstance(filepath, s3fs.core.S3File):
        granule_id = filepath.info()["name"].split("/", -1)[-1].split(".", -1)[0]
    elif isinstance(filepath, HTTPFile):
        granule_id = filepath.path.split("/", -1)[-1].split(".", -1)[0]
    elif isinstance(filepath, BytesIO):
        print('Handling BytesIO input...')
        # Set a dummy granule_id since BytesIO has no filename
        granule_id = 'dummy_id'
    else:
        granule_id = 'dummy_id'


    # Read in Data as Xarray Datasets
    engine, wvl_group = "h5netcdf", None

    ds = xr.open_dataset(filepath, engine=engine)
    loc = xr.open_dataset(filepath, engine=engine, group="location")

    # Check if mineral dataset and read in groups (only ds/loc for minunc)

    if "L2B_MIN_" in granule_id:
        wvl_group = "mineral_metadata"
    elif "L2B_MINUNC" not in granule_id:
        wvl_group = "sensor_band_parameters"

    wvl = None

    if wvl_group:
        wvl = xr.open_dataset(filepath, engine=engine, group=wvl_group)

    # Building Flat Dataset from Components
    data_vars = {**ds.variables}

    # Format xarray coordinates based upon emit product (no wvl for mineral uncertainty)
    coords = {
        "downtrack": (["downtrack"], ds.downtrack.data),
        "crosstrack": (["crosstrack"], ds.crosstrack.data),
        **loc.variables,
    }

    product_band_map = {
        "L2B_MIN_": "name",
        "L2A_MASK_": "mask_bands",
        "L1B_OBS_": "observation_bands",
        "L2A_RFL_": "wavelengths",
        "L1B_RAD_": "wavelengths",
        "L2A_RFLUNCERT_": "wavelengths",
    }

    # if band := product_band_map.get(next((k for k in product_band_map.keys() if k in granule_id), 'unknown'), None):
    # coords['bands'] = wvl[band].data

    if wvl:
        coords = {**coords, **wvl.variables}

    out_xr = xr.Dataset(data_vars=data_vars, coords=coords, attrs=ds.attrs)
    out_xr.attrs["granule_id"] = granule_id

    if band := product_band_map.get(
        next((k for k in product_band_map.keys() if k in granule_id), "unknown"), None
    ):
        if "minerals" in list(out_xr.dims):
            out_xr = out_xr.swap_dims({"minerals": band})
            out_xr = out_xr.rename({band: "mineral_name"})
        else:
            out_xr = out_xr.swap_dims({"bands": band})

    # Apply Quality and Band Masks, set fill values to NaN
    for var in list(ds.data_vars):
        if qmask is not None:
            out_xr[var].data[qmask == 1] = -9999
        if unpacked_bmask is not None:
            out_xr[var].data[unpacked_bmask == 1] = -9999

    if ortho is True:
        out_xr = ortho_xr(out_xr)
        out_xr.attrs["Orthorectified"] = "True"

    return out_xr


# Function to Calculate the center of pixel Lat and Lon Coordinates of the GLT grid
def get_pixel_center_coords(ds):
    """
    This function calculates the gridded latitude and longitude pixel centers for the dataset using the geotransform and GLT arrays.

    Parameters:
    ds: an emit dataset opened with emit_xarray function

    Returns:
    x_geo, y_geo: longitude and latitude pixel centers of glt (gridded data)

    """
    # Retrieve GLT
    GT = ds.geotransform
    # Get Shape of GLT
    dim_x = ds.glt_x.shape[1]
    dim_y = ds.glt_y.shape[0]
    # Build Arrays containing pixel centers
    x_geo = (GT[0] + 0.5 * GT[1]) + np.arange(dim_x) * GT[1]
    y_geo = (GT[3] + 0.5 * GT[5]) + np.arange(dim_y) * GT[5]

    return x_geo, y_geo


# Function to Apply the GLT to an array
def apply_glt(ds_array, glt_array, fill_value=-9999, GLT_NODATA_VALUE=0):
    """
    This function applies the GLT array to a numpy array of either 2 or 3 dimensions.

    Parameters:
    ds_array: numpy array of the desired variable
    glt_array: a GLT array constructed from EMIT GLT data

    Returns:
    out_ds: a numpy array of orthorectified data.
    """

    # Build Output Dataset
    if ds_array.ndim == 2:
        ds_array = ds_array[:, :, np.newaxis]
    out_ds = np.full(
        (glt_array.shape[0], glt_array.shape[1], ds_array.shape[-1]),
        fill_value,
        dtype=np.float32,
    )
    valid_glt = np.all(glt_array != GLT_NODATA_VALUE, axis=-1)

    # Adjust for One based Index - make a copy to prevent decrementing multiple times inside ortho_xr when applying the glt to elev
    glt_array_copy = glt_array.copy()
    glt_array_copy[valid_glt] -= 1
    out_ds[valid_glt, :] = ds_array[
        glt_array_copy[valid_glt, 1], glt_array_copy[valid_glt, 0], :
    ]
    return out_ds


def ortho_xr(ds, GLT_NODATA_VALUE=0, fill_value=-9999):
    """
    This function uses `apply_glt` to create an orthorectified xarray dataset.

    Parameters:
    ds: an xarray dataset produced by emit_xarray
    GLT_NODATA_VALUE: no data value for the GLT tables, 0 by default
    fill_value: the fill value for EMIT datasets, -9999 by default

    Returns:
    ortho_ds: an orthocorrected xarray dataset.

    """
    # Build glt_ds

    glt_ds = np.nan_to_num(
        np.stack([ds["glt_x"].data, ds["glt_y"].data], axis=-1), nan=GLT_NODATA_VALUE
    ).astype(int)

    # List Variables
    var_list = list(ds.data_vars)

    # Remove flat field from data vars - the flat field is only useful with additional information before orthorectification
    if "flat_field_update" in var_list:
        var_list.remove("flat_field_update")

    # Create empty dictionary for orthocorrected data vars
    data_vars = {}

    # Extract Rawspace Dataset Variable Values (Typically Reflectance)
    for var in var_list:
        raw_ds = ds[var].data
        var_dims = ds[var].dims
        # Apply GLT to dataset
        out_ds = apply_glt(raw_ds, glt_ds, GLT_NODATA_VALUE=GLT_NODATA_VALUE)

        # Update variables - Only works for 2 or 3 dimensional arays
        if raw_ds.ndim == 2:
            out_ds = out_ds.squeeze()
            data_vars[var] = (["latitude", "longitude"], out_ds)
        else:
            data_vars[var] = (["latitude", "longitude", var_dims[-1]], out_ds)

        del raw_ds

    # Calculate Lat and Lon Vectors
    lon, lat = get_pixel_center_coords(
        ds
    )  # Reorder this function to make sense in case of multiple variables

    # Apply GLT to elevation
    elev_ds = apply_glt(ds["elev"].data, glt_ds)

    # Delete glt_ds - no longer needed
    del glt_ds

    # Create Coordinate Dictionary
    coords = {
        "latitude": (["latitude"], lat),
        "longitude": (["longitude"], lon),
        **ds.coords,
    }  # unpack to add appropriate coordinates

    # Remove Unnecessary Coords
    for key in ["downtrack", "crosstrack", "lat", "lon", "glt_x", "glt_y", "elev"]:
        del coords[key]

    # Add Orthocorrected Elevation
    coords["elev"] = (["latitude", "longitude"], np.squeeze(elev_ds))

    # Build Output xarray Dataset and assign data_vars array attributes
    out_xr = xr.Dataset(data_vars=data_vars, coords=coords, attrs=ds.attrs)

    del out_ds
    # Assign Attributes from Original Datasets
    for var in var_list:
        out_xr[var].attrs = ds[var].attrs
    out_xr.coords["latitude"].attrs = ds["lat"].attrs
    out_xr.coords["longitude"].attrs = ds["lon"].attrs
    out_xr.coords["elev"].attrs = ds["elev"].attrs

    # Add Spatial Reference in recognizable format
    out_xr.rio.write_crs(ds.spatial_ref, inplace=True)

    return out_xr


def quality_mask(filepath, quality_bands):
    """
    This function builds a single layer mask to apply based on the bands selected from an EMIT L2A Mask file.

    Parameters:
    filepath: an EMIT L2A Mask netCDF file.
    quality_bands: a list of bands (quality flags only) from the mask file that should be used in creation of  mask.

    Returns:
    qmask: a numpy array that can be used with the emit_xarray function to apply a quality mask.
    """
    # Open Dataset
    mask_ds = xr.open_dataset(filepath, engine="h5netcdf")
    # Open Sensor band Group
    mask_parameters_ds = xr.open_dataset(
        filepath, engine="h5netcdf", group="sensor_band_parameters"
    )
    # Print Flags used
    flags_used = mask_parameters_ds["mask_bands"].data[quality_bands]
    print(f"Flags used: {flags_used}")
    # Check for data bands and build mask
    if any(x in quality_bands for x in [5, 6]):
        err_str = f"Selected flags include a data band (5 or 6) not just flag bands"
        raise AttributeError(err_str)
    else:
        qmask = np.sum(mask_ds["mask"][:, :, quality_bands].values, axis=-1)
        qmask[qmask > 1] = 1
    return qmask


def band_mask(filepath):
    """
    This function unpacks the packed band mask to apply to the dataset. Can be used manually or as an input in the emit_xarray() function.

    Parameters:
    filepath: an EMIT L2A Mask netCDF file.
    packed_bands: the 'packed_bands' dataset from the EMIT L2A Mask file.

    Returns:
    band_mask: a numpy array that can be used with the emit_xarray function to apply a band mask.
    """
    # Open Dataset
    mask_ds = xr.open_dataset(filepath, engine="h5netcdf")
    # Open band_mask and convert to uint8
    bmask = mask_ds.band_mask.data.astype("uint8")
    # Print Flags used
    unpacked_bmask = np.unpackbits(bmask, axis=-1)
    # Remove bands > 285
    unpacked_bmask = unpacked_bmask[:, :, 0:285]
    # Check for data bands and build mask
    return unpacked_bmask


def write_envi(
    xr_ds,
    output_dir,
    overwrite=False,
    extension=".img",
    interleave="BIL",
    glt_file=False,
):
    """
    This function takes an EMIT dataset read into an xarray dataset using the emit_xarray function and then writes an ENVI file and header. Does not work for L2B MIN.

    Parameters:
    xr_ds: an EMIT dataset read into xarray using the emit_xarray function.
    output_dir: output directory
    overwrite: overwrite existing file if True
    extension: the file extension for the envi formatted file, .img by default.
    glt_file: also create a GLT ENVI file for later use to reproject

    Returns:
    envi_ds: file in the output directory
    glt_ds: file in the output directory

    """
    # Check if xr_ds has been orthorectified, raise exception if it has been but GLT is still requested
    if (
        "Orthorectified" in xr_ds.attrs.keys()
        and xr_ds.attrs["Orthorectified"] == "True"
        and glt_file == True
    ):
        raise Exception("Data is already orthorectified.")

    # Typemap dictionary for ENVI files
    envi_typemap = {
        "uint8": 1,
        "int16": 2,
        "int32": 3,
        "float32": 4,
        "float64": 5,
        "complex64": 6,
        "complex128": 9,
        "uint16": 12,
        "uint32": 13,
        "int64": 14,
        "uint64": 15,
    }

    # Get CRS/geotransform for creation of Orthorectified ENVI file or optional GLT file
    gt = xr_ds.attrs["geotransform"]
    mapinfo = (
        "{Geographic Lat/Lon, 1, 1, "
        + str(gt[0])
        + ", "
        + str(gt[3])
        + ", "
        + str(gt[1])
        + ", "
        + str(gt[5] * -1)
        + ", WGS-84, units=Degrees}"
    )

    # This creates the coordinate system string
    # hard-coded replacement of wkt crs could probably be improved, though should be the same for all EMIT datasets
    csstring = '{ GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]] }'
    # List data variables (typically reflectance/radiance)
    var_names = list(xr_ds.data_vars)

    # Loop through variable names
    for var in var_names:
        # Define output filename
        output_name = os.path.join(output_dir, xr_ds.attrs["granule_id"] + "_" + var)

        nbands = 1
        if len(xr_ds[var].data.shape) > 2:
            nbands = xr_ds[var].data.shape[2]

        # Start building metadata
        metadata = {
            "lines": xr_ds[var].data.shape[0],
            "samples": xr_ds[var].data.shape[1],
            "bands": nbands,
            "interleave": interleave,
            "header offset": 0,
            "file type": "ENVI Standard",
            "data type": envi_typemap[str(xr_ds[var].data.dtype)],
            "byte order": 0,
            "data ignore value": -9999,
        }

        for key in list(xr_ds.attrs.keys()):
            if key == "summary":
                metadata["description"] = xr_ds.attrs[key]
            elif key not in ["geotransform", "spatial_ref"]:
                metadata[key] = f"{{ {xr_ds.attrs[key]} }}"

        # List all variables in dataset (including coordinate variables)
        meta_vars = list(xr_ds.variables)

        # Add band parameter information to metadata (ie wavelengths/obs etc.)
        for m in meta_vars:
            if m == "wavelengths" or m == "radiance_wl":
                metadata["wavelength"] = np.array(xr_ds[m].data).astype(str).tolist()
            elif m == "fwhm" or m == "radiance_fwhm":
                metadata["fwhm"] = np.array(xr_ds[m].data).astype(str).tolist()
            elif m == "good_wavelengths":
                metadata["good_wavelengths"] = (
                    np.array(xr_ds[m].data).astype(int).tolist()
                )
            elif m == "observation_bands":
                metadata["band names"] = np.array(xr_ds[m].data).astype(str).tolist()
            elif m == "mask_bands":
                if var == "band_mask":
                    metadata["band names"] = [
                        "packed_bands_" + bn
                        for bn in np.arange(285 / 8).astype(str).tolist()
                    ]
                else:
                    metadata["band names"] = (
                        np.array(xr_ds[m].data).astype(str).tolist()
                    )
            if "wavelength" in list(metadata.keys()) and "band names" not in list(
                metadata.keys()
            ):
                metadata["band names"] = metadata["wavelength"]

        # Add CRS/mapinfo if xarray dataset has been orthorectified
        if (
            "Orthorectified" in xr_ds.attrs.keys()
            and xr_ds.attrs["Orthorectified"] == "True"
        ):
            metadata["coordinate system string"] = csstring
            metadata["map info"] = mapinfo

        # Write Variables as ENVI Output
        envi_ds = envi.create_image(
            envi_header(output_name), metadata, ext=extension, force=overwrite
        )
        mm = envi_ds.open_memmap(interleave="bip", writable=True)

        dat = xr_ds[var].data

        if len(dat.shape) == 2:
            dat = dat.reshape((dat.shape[0], dat.shape[1], 1))

        mm[...] = dat

    # Create GLT Metadata/File
    if glt_file == True:
        # Output Name
        glt_output_name = os.path.join(
            output_dir, xr_ds.attrs["granule_id"] + "_" + "glt"
        )

        # Write GLT Metadata
        glt_metadata = metadata

        # Remove Unwanted Metadata
        glt_metadata.pop("wavelength", None)
        glt_metadata.pop("fwhm", None)

        # Replace Metadata
        glt_metadata["lines"] = xr_ds["glt_x"].data.shape[0]
        glt_metadata["samples"] = xr_ds["glt_x"].data.shape[1]
        glt_metadata["bands"] = 2
        glt_metadata["data type"] = envi_typemap["int32"]
        glt_metadata["band names"] = ["glt_x", "glt_y"]
        glt_metadata["coordinate system string"] = csstring
        glt_metadata["map info"] = mapinfo

        # Write GLT Outputs as ENVI File
        glt_ds = envi.create_image(
            envi_header(glt_output_name), glt_metadata, ext=extension, force=overwrite
        )
        mmglt = glt_ds.open_memmap(interleave="bip", writable=True)
        mmglt[...] = np.stack(
            (xr_ds["glt_x"].values, xr_ds["glt_y"].values), axis=-1
        ).astype("int32")


def envi_header(inputpath):
    """
    Convert a envi binary/header path to a header, handling extensions
    Args:
        inputpath: path to envi binary file
    Returns:
        str: the header file associated with the input reference.
    """
    if (
        os.path.splitext(inputpath)[-1] == ".img"
        or os.path.splitext(inputpath)[-1] == ".dat"
        or os.path.splitext(inputpath)[-1] == ".raw"
    ):
        # headers could be at either filename.img.hdr or filename.hdr.  Check both, return the one that exists if it
        # does, if not return the latter (new file creation presumed).
        hdrfile = os.path.splitext(inputpath)[0] + ".hdr"
        if os.path.isfile(hdrfile):
            return hdrfile
        elif os.path.isfile(inputpath + ".hdr"):
            return inputpath + ".hdr"
        return hdrfile
    elif os.path.splitext(inputpath)[-1] == ".hdr":
        return inputpath
    else:
        return inputpath + ".hdr"


def spatial_subset(ds, gdf):
    """
    Uses a geodataframe containing polygon geometry to clip the GLT of an emit dataset read with emit_xarray, then uses the min/max downtrack and crosstrack
    indices to subset the extent of the dataset in rawspace, masking areas outside the provided spatial geometry. Uses rioxarray's clip function.

    Parameters:
    ds: an emit dataset read into xarray using the emit_xarray function.
    gdf: a geodataframe.

    Returns:
    clipped_ds: an xarray dataset clipped to the extent of the provided geodataframe that can be orthorectified with ortho_xr.
    """
    # Reformat the GLT
    lon, lat = get_pixel_center_coords(ds)
    data_vars = {
        "glt_x": (["latitude", "longitude"], ds.glt_x.data),
        "glt_y": (["latitude", "longitude"], ds.glt_y.data),
    }
    coords = {
        "latitude": (["latitude"], lat),
        "longitude": (["longitude"], lon),
        "ortho_y": (["latitude"], ds.ortho_y.data),
        "ortho_x": (["longitude"], ds.ortho_x.data),
    }
    glt_ds = xr.Dataset(data_vars=data_vars, coords=coords, attrs=ds.attrs)
    glt_ds.rio.write_crs(glt_ds.spatial_ref, inplace=True)

    # Clip the emit glt
    clipped = glt_ds.rio.clip(gdf.geometry.values, gdf.crs, all_touched=True)
    # Get the clipped geotransform
    clipped_gt = np.array(
        [float(i) for i in clipped["spatial_ref"].GeoTransform.split(" ")]
    )

    valid_gltx = clipped.glt_x.data > 0
    valid_glty = clipped.glt_y.data > 0
    # Get the subset indices, -1 to convert to 0-based
    subset_down = [
        int(np.min(clipped.glt_y.data[valid_glty]) - 1),
        int(np.max(clipped.glt_y.data[valid_glty]) - 1),
    ]
    subset_cross = [
        int(np.min(clipped.glt_x.data[valid_gltx]) - 1),
        int(np.max(clipped.glt_x.data[valid_gltx]) - 1),
    ]

    # print(subset_down, subset_cross)

    crosstrack_mask = (ds.crosstrack >= subset_cross[0]) & (
        ds.crosstrack <= subset_cross[-1]
    )

    downtrack_mask = (ds.downtrack >= subset_down[0]) & (
        ds.downtrack <= subset_down[-1]
    )

    # Mask Areas outside of crosstrack and downtrack covered by the shape
    clipped_ds = ds.where((crosstrack_mask & downtrack_mask), drop=True)
    # Replace Full dataset geotransform with clipped geotransform
    clipped_ds.attrs["geotransform"] = clipped_gt

    # Drop unnecessary vars from dataset
    clipped_ds = clipped_ds.drop_vars(["glt_x", "glt_y", "downtrack", "crosstrack"])

    # Re-index the GLT to the new array
    glt_x_data = np.maximum(clipped.glt_x.data - subset_cross[0], 0)
    glt_y_data = np.maximum(clipped.glt_y.data - subset_down[0], 0)

    clipped_ds = clipped_ds.assign_coords(
        {
            "glt_x": (["ortho_y", "ortho_x"], np.nan_to_num(glt_x_data)),
            "glt_y": (["ortho_y", "ortho_x"], np.nan_to_num(glt_y_data)),
        }
    )
    clipped_ds = clipped_ds.assign_coords(
        {
            "downtrack": (
                ["downtrack"],
                np.arange(0, clipped_ds[list(ds.data_vars.keys())[0]].shape[0]),
            ),
            "crosstrack": (
                ["crosstrack"],
                np.arange(0, clipped_ds[list(ds.data_vars.keys())[0]].shape[1]),
            ),
        }
    )

    clipped_ds.attrs["subset_downtrack_range"] = subset_down
    clipped_ds.attrs["subset_crosstrack_range"] = subset_cross

    return clipped_ds


def is_adjacent(scene: str, same_orbit: list):
    """
    This function makes a list of scene numbers from the same orbit as integers and checks
    if they are adjacent/sequential.
    """
    scene_nums = [int(scene.split(".")[-2].split("_")[-1]) for scene in same_orbit]
    return all(b - a == 1 for a, b in zip(scene_nums[:-1], scene_nums[1:]))


def merge_emit(datasets: dict, gdf: gpd.GeoDataFrame):
    """
    A function to merge xarray datasets formatted using emit_xarray. This could probably be improved,
    lots of shuffling data around to keep in xarray and get it to merge properly. Note: GDF may only work with a
    single geometry.
    """
    nested_data_arrays = {}
    # loop over datasets
    for dataset in datasets:
        # create dictionary of arrays for each dataset

        # create dictionary of 1D variables, which should be consistent across datasets
        one_d_arrays = {}

        # Dictionary of variables to merge
        data_arrays = {}
        # Loop over variables in dataset including elevation
        for var in list(datasets[dataset].data_vars) + ["elev"]:
            # Get 1D for this variable and add to dictionary
            if not one_d_arrays:
                # These should be an array describing the others (wavelengths, mask_bands, etc.)
                one_dim = [
                    item
                    for item in list(datasets[dataset].coords)
                    if item not in ["latitude", "longitude", "spatial_ref"]
                    and len(datasets[dataset][item].dims) == 1
                ]
                # print(one_dim)
                for od in one_dim:
                    one_d_arrays[od] = datasets[dataset].coords[od].data

                # Update format for merging - This could probably be improved
            da = datasets[dataset][var].reset_coords("elev", drop=False)
            da = da.rename({"latitude": "y", "longitude": "x"})
            if len(da.dims) == 3:
                if any(item in list(da.coords) for item in one_dim):
                    da = da.drop_vars(one_dim)
                da = da.drop_vars("elev")
                da = da.to_array(name=var).squeeze("variable", drop=True)
                da = da.transpose(da.dims[-1], da.dims[0], da.dims[1])
                # print(da.dims)
            if var == "elev":
                da = da.to_array(name=var).squeeze("variable", drop=True)
            data_arrays[var] = da
            nested_data_arrays[dataset] = data_arrays

            # Transpose the nested arrays dict. This is horrible to read, but works to pair up variables (ie mask) from the different granules
    transposed_dict = {
        inner_key: {
            outer_key: inner_dict[inner_key]
            for outer_key, inner_dict in nested_data_arrays.items()
        }
        for inner_key in nested_data_arrays[next(iter(nested_data_arrays))]
    }

    # remove some unused data
    del nested_data_arrays, data_arrays, da

    # Merge the arrays using rioxarray.merge_arrays()
    merged = {}
    for _var in transposed_dict:
        merged[_var] = merge_arrays(
            list(transposed_dict[_var].values()),
            bounds=gdf.unary_union.bounds,
            nodata=-9999,
        )

    # Create a new xarray dataset from the merged arrays
    # Create Merged Dataset
    merged_ds = xr.Dataset(data_vars=merged, coords=one_d_arrays)
    # Rename x and y to longitude and latitude
    merged_ds = merged_ds.rename({"y": "latitude", "x": "longitude"})
    del transposed_dict, merged
    return merged_ds


def ortho_browse(url, glt, spatial_ref, geotransform, white_background=True):
    """
    Use an EMIT GLT, geotransform, and spatial ref to orthorectify a browse image. (browse images are in native resolution)
    """
    # Read Data
    data = io.imread(url)
    # Orthorectify using GLT and transpose so band is first dimension
    if white_background == True:
        fill = 255
    else:
        fill = 0
    ortho_data = apply_glt(data, glt, fill_value=fill).transpose(2, 0, 1)
    coords = {
        "y": (
            ["y"],
            (geotransform[3] + 0.5 * geotransform[5])
            + np.arange(glt.shape[0]) * geotransform[5],
        ),
        "x": (
            ["x"],
            (geotransform[0] + 0.5 * geotransform[1])
            + np.arange(glt.shape[1]) * geotransform[1],
        ),
    }
    ortho_data = ortho_data.astype(int)
    ortho_data[ortho_data == -1] = 0
    # Place in xarray.datarray
    da = xr.DataArray(ortho_data, dims=["band", "y", "x"], coords=coords)
    da.rio.write_crs(spatial_ref, inplace=True)
    return da




def save_spectra_csv(spectra_data, csv_file_path):
    '''
    takes dictionary prepared during notebook and saves to a csv
    
    '''

    csv_data = []

    # Loop through each point
    for point_id, data in spectra_data.items():

        wavelengths = data['Wavelength']
        reflectance = data['Reflectance']
        lat = data['lat']
        lon = data['lon']
        
        
        # Create a dictionary for this row of data
        for i in range(len(wavelengths)):
            row = {
                'ID': point_id,
                'Latitude': lat,
                'Longitude': lon,
                'Wavelength': wavelengths[i],
                'Reflectance': reflectance[i],
            }
            csv_data.append(row)

    df = pd.DataFrame(csv_data)
    df.to_csv(csv_file_path, index=False)

    return






def select_pixels(ds, coords):
    """
    Function to select pixels from an EMIT dataset and compute reflectance and standard deviation.
    
    Parameters:
    - ds: xarray.Dataset containing the EMIT data.
    - coords: List of tuples containing latitude and longitude coordinates.
    - neighbor: Boolean flag to indicate whether to compute neighborhood stats (True for 3x3 window).
    
    Returns:
    - spectra_data: Dictionary containing the spectra data for each point.
    """

    reflectance = ds['reflectance']
    wavelengths = ds['wavelengths'].values

    # Prepare an empty dictionary to store spectra data
    spectra_data = {}

    for i, (lat, lon) in enumerate(coords):
        lat_idx = np.abs(ds.latitude.values - lat).argmin()
        lon_idx = np.abs(ds.longitude.values - lon).argmin()

        # id
        s_id = f'Pt{i+1}'

        # Generate a random color 
        random_color = np.random.rand(3,) 

        reflectance_at_pixel = reflectance[lat_idx, lon_idx, :].values

        # Remove bad bands
        reflectance_at_pixel[reflectance_at_pixel < 0] = np.nan

        # Save 
        spectra_data[s_id] = {
            'lat_idx': lat_idx,
            'lon_idx': lon_idx,
            'lat': lat,
            'lon': lon,
            'Wavelength': wavelengths,
            'Reflectance': reflectance_at_pixel,
            'Color': random_color 
        }

    return spectra_data










def dynamic_plot(reflectance, latitudes, longitudes, wavelengths, rgb_image_eq):
  
    img_height, img_width, bands = reflectance.shape

    # Variables to store points 
    points = []
    df = None
    can_add_points = False

    def on_click(event):
        nonlocal can_add_points
        if can_add_points and event.inaxes == ax1:  # Only click inside and "Add Points" mode
            x, y = int(round(event.xdata)), int(round(event.ydata))
            if 0 <= x < img_width and 0 <= y < img_height:
                color = [random.random(), random.random(), random.random()]
                points.append((x, y, color))
                ax1.plot(x, y, 'o', color=color, markersize=5) 
                ax2.plot(wavelengths, reflectance[y, x, :], color=color, lw=1)  
                fig.canvas.draw_idle()

    # Function to save points and store DataFrame in the widget
    def save_points(event):
        nonlocal points, df
        if points:
            point_data = []
            p = 0
            for x, y, color in points:
                lat = latitudes[y]
                lon = longitudes[x]
                reflectance_values = reflectance[y, x, :].values 

                for i, wavelength in enumerate(wavelengths):
                    point_entry = {
                        'ID': f'Pt{p+1}',
                        'lat': lat,
                        'lon': lon,
                        'Wavelength': wavelength,
                        'Reflectance': reflectance_values[i]
                    }
                    point_data.append(point_entry)
                p += 1
            
            # Save the DataFrame to the widget
            widget.data_frame = pd.DataFrame(point_data)
            print("DataFrame saved to widget.")

    # Function to clear points 
    def clear_points(event):
        nonlocal points, df
        points = [] 
        df = None 
        ax1.clear() 
        ax2.clear() 
        ax1.imshow(rgb_image_eq) 
        ax1.set_xticks([])  
        ax1.set_yticks([]) 
        ax2.set_xlabel('Wavelength (nm)', color='yellow', fontsize=10, 
                       bbox=dict(facecolor='black', edgecolor='yellow', boxstyle='round,pad=0.3'))
        ax2.set_ylabel('Reflectance', color='yellow', fontsize=10, 
                       bbox=dict(facecolor='black', edgecolor='yellow', boxstyle='round,pad=0.3'))
        ax2.set_facecolor('black')

        for spine in ax2.spines.values():
            spine.set_edgecolor('yellow')
            spine.set_linewidth(2)
        ax2.tick_params(axis='both', colors='yellow', labelcolor='yellow')
        for label in ax2.get_xticklabels() + ax2.get_yticklabels():
            label.set_bbox(dict(facecolor='black', edgecolor='yellow', boxstyle='round,pad=0.1'))
        fig.canvas.draw_idle()

    # Function to activate add points mode
    def activate_add_point(event):
        nonlocal can_add_points
        can_add_points = True 

    # Function to deactivate add points mode
    def deactivate_add_point(event):
        nonlocal can_add_points
        can_add_points = False  # Disable 

    # Create the figure and plot the image
    fig, ax1 = plt.subplots(figsize=(7, 5))
    img_display = ax1.imshow(rgb_image_eq)
    ax1.set_xticks([])  
    ax1.set_yticks([])  

    for spine in ax1.spines.values():
        spine.set_visible(False)

    ax2 = fig.add_axes([0.7, 0.1, 0.25, 0.3]) 
    spectrum_line, = ax2.plot([], [], lw=2, color='yellow')
    ax2.set_xlabel('Wavelength (nm)', color='yellow', fontsize=10, 
                   bbox=dict(facecolor='black', edgecolor='yellow', boxstyle='round,pad=0.3'))
    ax2.set_ylabel('Reflectance', color='yellow', fontsize=10, 
                   bbox=dict(facecolor='black', edgecolor='yellow', boxstyle='round,pad=0.3'))
    ax2.set_facecolor('black')

    for spine in ax2.spines.values():
        spine.set_edgecolor('yellow')  
        spine.set_linewidth(2) 

    ax2.tick_params(axis='both', colors='yellow', labelcolor='yellow')

    for label in ax2.get_xticklabels() + ax2.get_yticklabels():
        label.set_bbox(dict(facecolor='black', edgecolor='yellow', boxstyle='round,pad=0.1'))

    fig.canvas.mpl_connect('button_press_event', on_click)

    # Buttons
    add_point_button = Button(description="Add points")
    add_point_button.on_click(activate_add_point)

    deactivate_button = Button(description="Stop adding points")
    deactivate_button.on_click(deactivate_add_point)

    save_button = Button(description="Save points to `df`")
    save_button.on_click(save_points)

    clear_button = Button(description="Clear points")
    clear_button.on_click(clear_points)

    display(VBox([add_point_button, deactivate_button, save_button, clear_button]))

    # Function to update the spectral plot when the mouse moves
    def update_spectral_plot(x, y):
        spectrum = reflectance[y, x, :]
        spectrum[spectrum == -0.01] = np.nan
        spectrum_line.set_data(wavelengths, spectrum)
        ax2.relim()
        ax2.autoscale_view()
        fig.canvas.draw_idle()

    def on_mouse_move(event):
        if event.xdata is not None and event.ydata is not None:
            x, y = int(round(event.xdata)), int(round(event.ydata))
            if 0 <= x < img_width and 0 <= y < img_height:
                update_spectral_plot(x, y)
                lat = latitudes[y]
                lon = longitudes[x]
                ax1.set_title(f'Latitude: {lat:.4f}, Longitude: {lon:.4f}')

    fig.canvas.mpl_connect('motion_notify_event', on_mouse_move)

    plt.show()

    # Create the widget to hold pandas
    widget = widgets.Widget()
    widget.data_frame = None  
    return widget
