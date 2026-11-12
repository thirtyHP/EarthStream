# NASA Earthdata platforms, in the same registry style as SatStream's providers.py.
# Each entry says where to search (CMR short name and provider), how to recognise
# each layer in a granule's file list, what Load Visual / Multispectral / Panchromatic
# should do, and which analysis products the platform supports.
#
# Band keys are shared across platforms (blue, green, red, nir, swir1, swir2...) so
# the index formulas in processing.py work on HLS Landsat and HLS Sentinel alike.
# For HLS Sentinel, "nir" is the narrow NIR band (B8A), which is the one NASA
# harmonises with Landsat's NIR, so indices stay comparable between the two.

CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.json"
EDL_TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
EDL_PROFILE_URL = "https://urs.earthdata.nasa.gov/profile"

_HLS_COMMON = dict(
    provider="LPCLOUD",
    supports_cloud=True,
    kind="reflectance",
    streamable=True,
    native_res=30,
    nodata=-9999,
    scale=0.0001,
    mask=dict(band="fmask", type="hls_fmask", nodata=255),
    visual=("red", "green", "blue"),
    visual_range=(0, 1800),
    pan=None,
    pan_message="HLS has no panchromatic band. The 15 m pan band only exists in Landsat Level-1 products.",
    tile_regex=r"\.T(\d{2}[A-Z]{3})\.",
    download_regex=r"\.tif$",
)

PROVIDERS = {
    "HLS Landsat (L30)": dict(
        _HLS_COMMON,
        short_name="HLSL30",
        version="2.0",
        gsd_label="30 m",
        bands={
            "coastal": r"\.B01\.tif$",
            "blue": r"\.B02\.tif$",
            "green": r"\.B03\.tif$",
            "red": r"\.B04\.tif$",
            "nir": r"\.B05\.tif$",
            "swir1": r"\.B06\.tif$",
            "swir2": r"\.B07\.tif$",
            "cirrus": r"\.B09\.tif$",
            "tir1": r"\.B10\.tif$",
            "tir2": r"\.B11\.tif$",
            "fmask": r"\.Fmask\.tif$",
        },
        ms_bands=["coastal", "blue", "green", "red", "nir", "swir1", "swir2"],
        ms_default=("nir", "red", "green"),
        note="Harmonized Landsat 8/9 surface reflectance, 30 m, every 2-3 days with HLS Sentinel. "
             "Streams as Cloud-Optimized GeoTIFFs once you sign in with Earthdata Login.",
    ),
    "HLS Sentinel-2 (S30)": dict(
        _HLS_COMMON,
        short_name="HLSS30",
        version="2.0",
        gsd_label="30 m",
        bands={
            "coastal": r"\.B01\.tif$",
            "blue": r"\.B02\.tif$",
            "green": r"\.B03\.tif$",
            "red": r"\.B04\.tif$",
            "rededge1": r"\.B05\.tif$",
            "rededge2": r"\.B06\.tif$",
            "rededge3": r"\.B07\.tif$",
            "nir_broad": r"\.B08\.tif$",
            "nir": r"\.B8A\.tif$",
            "watervapor": r"\.B09\.tif$",
            "cirrus": r"\.B10\.tif$",
            "swir1": r"\.B11\.tif$",
            "swir2": r"\.B12\.tif$",
            "fmask": r"\.Fmask\.tif$",
        },
        ms_bands=["coastal", "blue", "green", "red", "rededge1", "rededge2", "rededge3",
                  "nir_broad", "nir", "swir1", "swir2"],
        ms_default=("nir", "red", "green"),
        note="Harmonized Sentinel-2 surface reflectance resampled to 30 m on the same grid as HLS Landsat. "
             "Combine both HLS platforms for the densest time series.",
    ),
    "OPERA Surface Water (DSWx-HLS)": dict(
        short_name="OPERA_L3_DSWX-HLS_V1",
        version=None,
        provider="POCLOUD",
        supports_cloud=False,
        kind="thematic",
        streamable=True,
        native_res=30,
        gsd_label="30 m",
        nodata=255,
        scale=1.0,
        mask=None,
        bands={
            "wtr": r"_B01_WTR\.tif$",
            "bwtr": r"_B02_BWTR\.tif$",
            "conf": r"_B03_CONF\.tif$",
            "diag": r"_B04_DIAG\.tif$",
            "wtr1": r"_B05_WTR-1\.tif$",
            "wtr2": r"_B06_WTR-2\.tif$",
            "land": r"_B07_LAND\.tif$",
            "shad": r"_B08_SHAD\.tif$",
            "cloud": r"_B09_CLOUD\.tif$",
            "dem": r"_B10_DEM\.tif$",
        },
        visual=("wtr",),
        visual_style="palette",
        ms_bands=None,
        pan=None,
        pan_message="DSWx is a classified water product, so there is no panchromatic layer.",
        ms_message="DSWx is a classified water product. Load Visual shows the water classification layer.",
        tile_regex=r"_T(\d{2}[A-Z]{3})_",
        download_regex=r"\.tif$",
        note="Open and partial surface water classified from HLS (JPL/OPERA), distributed by PO.DAAC. "
             "The Analyze tools turn a stack of scenes into water frequency.",
    ),
    "OPERA Disturbance (DIST-ALERT-HLS)": dict(
        short_name="OPERA_L3_DIST-ALERT-HLS_V1",
        version=None,
        provider="LPCLOUD",
        supports_cloud=False,
        kind="thematic",
        streamable=True,
        native_res=30,
        gsd_label="30 m",
        nodata=255,
        scale=1.0,
        mask=None,
        bands={
            "veg_dist_status": r"_VEG-DIST-STATUS\.tif$",
            "veg_anom_max": r"_VEG-ANOM-MAX\.tif$",
            "veg_dist_conf": r"_VEG-DIST-CONF\.tif$",
            "veg_dist_date": r"_VEG-DIST-DATE\.tif$",
            "veg_ind": r"_VEG-IND\.tif$",
            "veg_anom": r"_VEG-ANOM\.tif$",
            "gen_dist_status": r"_GEN-DIST-STATUS\.tif$",
            "data_mask": r"_DATA-MASK\.tif$",
        },
        visual=("veg_dist_status",),
        visual_style="palette",
        ms_bands=None,
        pan=None,
        pan_message="DIST-ALERT is a disturbance product, so there is no panchromatic layer.",
        ms_message="DIST-ALERT is a disturbance product. Load Visual shows vegetation disturbance status.",
        tile_regex=r"_T(\d{2}[A-Z]{3})_",
        download_regex=r"\.tif$",
        note="Vegetation cover loss alerts from HLS (UMD GLAD / JPL OPERA). Useful for fire scars, "
             "clearing and storm damage.",
    ),
    "ECOSTRESS Land Surface Temp (L2T)": dict(
        short_name="ECO_L2T_LSTE",
        version="002",
        provider="LPCLOUD",
        supports_cloud=False,
        kind="lst",
        streamable=True,
        native_res=70,
        gsd_label="70 m",
        nodata=None,
        scale=1.0,
        mask=dict(band="cloud", type="ecostress_cloud", nodata=255),
        bands={
            "lst": r"_LST\.tif$",
            "lst_err": r"_LST_err\.tif$",
            "qc": r"_QC\.tif$",
            "emiswb": r"_EmisWB\.tif$",
            "height": r"_height\.tif$",
            "cloud": r"_cloud\.tif$",
            "water": r"_water\.tif$",
            "view_zenith": r"_view_zenith\.tif$",
        },
        visual=("lst",),
        visual_style="lst",
        visual_range=(270, 330),
        ms_bands=None,
        pan=None,
        pan_message="ECOSTRESS is a thermal product, so there is no panchromatic layer.",
        ms_message="ECOSTRESS L2T has one temperature layer. Load Visual shows LST in kelvin.",
        tile_regex=r"_(\d{2}[A-Z]{3})_\d{8}T",
        download_regex=r"\.tif$",
        note="Land surface temperature from the ISS, 70 m, irregular overpass times (day and night). "
             "Cloud pixels are masked using the scene's cloud layer.",
    ),
    "ASTER GDEM v3": dict(
        short_name="ASTGTM",
        version="003",
        provider="LPCLOUD",
        supports_cloud=False,
        kind="dem",
        streamable=True,
        native_res=30,
        gsd_label="1 arc-second",
        nodata=-9999,
        scale=1.0,
        mask=None,
        bands={
            "dem": r"_dem\.tif$",
            "num": r"_num\.tif$",
        },
        visual=("dem",),
        visual_style="hillshade",
        ms_bands=None,
        pan=None,
        pan_message="ASTER GDEM is an elevation model, so there is no panchromatic layer.",
        ms_message="ASTER GDEM is an elevation model. Load Visual shows a hillshade of the DEM.",
        tile_regex=r"_([NS]\d{2}[EW]\d{3})_",
        download_regex=r"_dem\.tif$",
        note="Global 1x1 degree elevation tiles, 83N to 83S. Search returns every tile touching the "
             "search area; Analyze mosaics the selected tiles before computing slope or hillshade.",
    ),
    "NASADEM (SRTM reprocessed)": dict(
        short_name="NASADEM_HGT",
        version="001",
        provider="LPCLOUD",
        supports_cloud=False,
        kind="dem",
        streamable=True,
        zipped=True,
        native_res=30,
        gsd_label="1 arc-second",
        nodata=-32768,
        scale=1.0,
        mask=None,
        bands={
            "dem": r"\.zip$",
        },
        visual=("dem",),
        visual_style="hillshade",
        ms_bands=None,
        pan=None,
        pan_message="NASADEM is an elevation model, so there is no panchromatic layer.",
        ms_message="NASADEM is an elevation model. Load Visual shows a hillshade of the DEM.",
        tile_regex=r"_([ns]\d{2}[ew]\d{3})\.zip",
        download_regex=r"\.zip$",
        note="SRTM elevation reprocessed by NASA, 60N to 56S. Tiles are zipped .hgt files, read in place "
             "through GDAL's /vsizip/ without downloading the whole archive.",
    ),
    "MODIS Vegetation Indices (MOD13Q1)": dict(
        short_name="MOD13Q1",
        version="061",
        provider="LPCLOUD",
        supports_cloud=False,
        kind="archive",
        streamable=False,
        native_res=250,
        gsd_label="250 m",
        nodata=None,
        scale=1.0,
        mask=None,
        bands={"hdf": r"\.hdf$"},
        visual=None,
        ms_bands=None,
        pan=None,
        pan_message="MOD13Q1 has no panchromatic layer.",
        stream_message="MOD13Q1 granules are HDF4 files, which can't be read over HTTP. "
                       "Use Download Selected: the 250 m NDVI layer is added to the map after download.",
        tile_regex=r"\.(h\d{2}v\d{2})\.",
        download_regex=r"\.hdf$",
        note="16-day Terra MODIS NDVI and EVI at 250 m since 2000. Download only (HDF4); the NDVI "
             "subdataset loads automatically when the download finishes.",
    ),
}


# Analysis products. "inputs" are band keys; the formula lives in processing.py under the same name.
# "ramp" is a QGIS style ramp name, "range" the default display stretch, "units" goes in the legend.
OPTICAL_PRODUCTS = {
    "NDVI": dict(inputs=["nir", "red"], ramp="RdYlGn", range=(-0.2, 0.9), units="index",
                 tip="Vegetation greenness"),
    "EVI": dict(inputs=["nir", "red", "blue"], ramp="RdYlGn", range=(-0.2, 0.8), units="index",
                tip="Vegetation index less prone to saturation in dense canopy"),
    "SAVI": dict(inputs=["nir", "red"], ramp="RdYlGn", range=(-0.2, 0.8), units="index",
                 tip="Vegetation index adjusted for bright soil"),
    "NDWI": dict(inputs=["green", "nir"], ramp="Blues", range=(-0.6, 0.6), units="index",
                 tip="Open water (McFeeters)"),
    "MNDWI": dict(inputs=["green", "swir1"], ramp="Blues", range=(-0.6, 0.8), units="index",
                  tip="Open water, better in built-up areas"),
    "NDMI": dict(inputs=["nir", "swir1"], ramp="BrBG", range=(-0.5, 0.6), units="index",
                 tip="Vegetation moisture"),
    "NBR": dict(inputs=["nir", "swir2"], ramp="RdYlGn", range=(-0.5, 0.8), units="index",
                tip="Burn severity (use Change to get dNBR)"),
    "NDBI": dict(inputs=["swir1", "nir"], ramp="Reds", range=(-0.4, 0.4), units="index",
                 tip="Built-up surfaces"),
}

PRODUCTS_BY_KIND = {
    "thematic_dswx": {
        "Surface water (%)": dict(inputs=["wtr"], ramp="Blues", range=(0, 100), units="%",
                                  tip="Per scene 0 or 100; averaged over time it becomes water frequency"),
    },
    "thematic_dist": {
        "Max vegetation loss anomaly (%)": dict(inputs=["veg_anom_max"], ramp="Reds", range=(0, 100),
                                                units="%", tip="Largest vegetation cover loss seen"),
        "Vegetation cover (%)": dict(inputs=["veg_ind"], ramp="Greens", range=(0, 100), units="%",
                                     tip="Current vegetation cover indicator"),
    },
    "lst": {
        "Land surface temperature (C)": dict(inputs=["lst"], ramp="Spectral", invert=True, range=(0, 50),
                                             units="degC", tip="Kelvin converted to Celsius"),
        "Wideband emissivity": dict(inputs=["emiswb"], ramp="Viridis", range=(0.9, 1.0), units="",
                                    tip="Surface emissivity"),
    },
    "dem": {
        "Elevation (m)": dict(inputs=["dem"], ramp="Spectral", invert=True, range=None, units="m",
                              tip="Mosaic of the selected tiles"),
        "Slope (degrees)": dict(inputs=["dem"], ramp="YlOrRd", range=(0, 45), units="deg",
                                tip="Computed on the projected mosaic"),
        "Hillshade": dict(inputs=["dem"], ramp="Greys", invert=True, range=(0, 255), units="",
                          tip="Sun from the northwest, 45 degrees up"),
    },
}

AGGREGATIONS = [
    ("Median", "Per-pixel median of every clear observation"),
    ("Mean", "Per-pixel mean"),
    ("Maximum", "Per-pixel maximum (a greenest-pixel composite for NDVI)"),
    ("Minimum", "Per-pixel minimum"),
    ("Std. deviation", "Per-pixel variability through time"),
    ("Valid count", "How many clear observations each pixel has"),
    ("Change (latest - earliest)", "Last clear value minus first clear value, per pixel (dNBR, dNDVI...)"),
]


def products_for(provider_name):
    cfg = PROVIDERS[provider_name]
    kind = cfg["kind"]
    if kind == "reflectance":
        prods = dict(OPTICAL_PRODUCTS)
        for key in cfg["ms_bands"]:
            prods["Band: %s" % key] = dict(inputs=[key], ramp="Greys", invert=True, range=(0, 0.4),
                                           units="reflectance", tip="Surface reflectance")
        return prods
    if kind == "thematic":
        if "DSWX" in cfg["short_name"]:
            return PRODUCTS_BY_KIND["thematic_dswx"]
        return PRODUCTS_BY_KIND["thematic_dist"]
    if kind in PRODUCTS_BY_KIND:
        return PRODUCTS_BY_KIND[kind]
    return {}
