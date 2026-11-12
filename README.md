# EarthStream

A QGIS plugin for searching, streaming, aggregating and analyzing NASA Earthdata raster imagery. It is the NASA counterpart to SatStream and follows the same workflow: pick a platform, sign in, set filters, search the map extent, then work from the Imagery Footprint table.

Version 0.1.0 · George Ehrlich · QGIS 3.28 to 4.x (Qt5 and Qt6)

## Install

1. In QGIS open **Plugins > Manage and Install Plugins > Install from ZIP**, choose `EarthStream-0.1.0.zip` and click **Install Plugin**.
2. Click the EarthStream toolbar button (or **Raster > EarthStream**). The panel docks on the right.

Nothing has to be pip-installed. The plugin uses only the Python standard library plus the GDAL and NumPy that ship with QGIS. Matplotlib is used for the time-series chart when it is present, and skipped quietly when it is not.

## Earthdata Login

Searching works without an account. Streaming and downloading need a free Earthdata Login account from [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov).

Enter your username and password and click **Sign In**. The plugin exchanges them for an Earthdata user token and never writes the password anywhere. You can also paste a token generated on your Earthdata profile page. With **Remember** ticked, the username and token are kept in QGIS settings (unencrypted, like SatStream's credentials) and restored the next time QGIS starts. Tokens expire after a few months; the panel shows the expiry date, and you just sign in again when it passes.

The token is attached only to NASA archive hosts (`*.earthdata.nasa.gov` and `*.earthdatacloud.nasa.gov`) using GDAL's per-path options, so no other web raster QGIS reads ever sees it. On GDAL builds older than 3.6, which lack per-path options, the header has to be global.

## Platforms

| Platform | CMR collection | Resolution | Streams | Analysis products |
|---|---|---|---|---|
| HLS Landsat (L30) | HLSL30 v2.0, LP DAAC | 30 m | Yes (COG) | NDVI, EVI, SAVI, NDWI, MNDWI, NDMI, NBR, NDBI, any band |
| HLS Sentinel-2 (S30) | HLSS30 v2.0, LP DAAC | 30 m | Yes (COG) | Same indices plus red-edge bands |
| OPERA Surface Water (DSWx-HLS) | OPERA_L3_DSWX-HLS_V1, PO.DAAC | 30 m | Yes (COG) | Surface water, water frequency over time |
| OPERA Disturbance (DIST-ALERT-HLS) | OPERA_L3_DIST-ALERT-HLS_V1, LP DAAC | 30 m | Yes (COG) | Max vegetation loss anomaly, vegetation cover |
| ECOSTRESS Land Surface Temp (L2T) | ECO_L2T_LSTE v002, LP DAAC | 70 m | Yes (COG) | LST in °C (cloud masked), wideband emissivity |
| ASTER GDEM v3 | ASTGTM v003, LP DAAC | 1″ | Yes (COG) | Elevation, slope, hillshade |
| NASADEM | NASADEM_HGT v001, LP DAAC | 1″ | Yes (zipped .hgt via /vsizip/) | Elevation, slope, hillshade |
| MODIS Vegetation Indices | MOD13Q1 v061, LP DAAC | 250 m | No (HDF4) | Download only; NDVI loads after download |

HLS Sentinel-2's narrow NIR band (B8A) is used as "nir" so indices from both HLS platforms are directly comparable and can be mixed in one composite.

## The panel

**Platform Selection** chooses the collection and shows a short note about it.

**Filters** set the cloud limit (HLS only, since it is the only family with granule cloud cover in CMR), the date range (ignored for DEMs), the result cap, and the **Area**: either the current map view or any polygon layer in the project. With a polygon layer, its selected features are used if any are selected, otherwise all of them. The same area drives search, clipping and analysis.

**Imagery Footprint** lists the granules (sortable, Ctrl or Shift click for several). Selecting rows highlights their outlines on the map. Double-clicking a row loads it.

**Tools**

- *Load Visual* streams a true-color composite for HLS, the water classification for DSWx, disturbance status for DIST-ALERT, LST for ECOSTRESS, or a hillshade for the DEMs.
- *Load Multispectral* streams every reflective HLS band as one layer, shown as false color (NIR, red, green). Change band assignments in the layer's symbology.
- *Load Panchromatic* explains that HLS has no pan band (it only exists in Landsat Level-1).
- *Download Selected* saves each granule into its own folder. With *Clip to search area* ticked it reads only the part of each file inside the area, in its native projection and resolution, which is usually a fraction of the full download. HLS downloads are stacked into a VRT and added to the map.
- *Clear Layers* removes everything the plugin added.

**Aggregate and Analyze** runs on the selected scenes:

- *Product* is the index or layer to compute.
- *Aggregate* is how dates are combined per pixel: median, mean, maximum, minimum, standard deviation, valid count, or change (latest clear value minus earliest clear value, which gives dNBR, dNDVI, warming and so on).
- *Resolution* defaults to the native pixel size. Large areas are automatically coarsened to stay under about 25 million pixels per layer, and the message bar says when that happened.
- *Mask* removes HLS Fmask clouds, adjacent pixels and shadows, and optionally snow/ice and water. For ECOSTRESS it applies the scene's cloud layer.
- *Outputs* are a composite GeoTIFF (styled and added to the map) and a time-series table with one row per date (valid %, mean, median, standard deviation, min, max, 10th and 90th percentiles), saved as CSV, added as a table layer and charted.

Granules from the same day and sensor are merged into one observation first, so an area spanning two HLS tiles counts once per date. Outputs go to `<Output folder>/analysis/`.

## How it works

The modules mirror SatStream's layout.

| File | Role |
|---|---|
| `providers.py` | Platform registry: CMR short names, band file patterns, display defaults, analysis products |
| `cmr_client.py` | Granule search on CMR (`granules.json`, paged with `CMR-Search-After`) |
| `credentials.py` | Earthdata Login tokens, GDAL streaming options, downloads that drop the token on the redirect to S3 |
| `tasks.py` | `QgsTask` workers for sign in, search, loading, downloads and analysis |
| `processing.py` | Analysis grid, Fmask decoding, index formulas, temporal aggregation, statistics, clipping (no QGIS imports) |
| `layers.py` | Layer styling and the footprint layer |
| `dockwidget.py` | The panel |
| `qt_compat.py` | Enum and field helpers that work on both Qt5 and Qt6 |

Rasters are never downloaded for loading or analysis. Every read goes through GDAL `/vsicurl/`, which fetches only the byte ranges and overview levels it needs from each Cloud-Optimized GeoTIFF. During analysis each date is warped onto one grid (the first scene's UTM zone, or the local UTM zone for geographic DEMs), masked and reduced to the product, then written to a disk-backed stack that is aggregated a block of rows at a time, so memory use stays flat however many dates are selected.

## Troubleshooting

- **"Could not open ... (check that you are signed in)"**: the token is missing or expired. Sign in again. If it persists for one DAAC, open your Earthdata profile and make sure that archive's applications are authorized.
- **Downloads land on a login page**: same cause; the plugin reports this rather than saving an HTML page as a .tif.
- **MODIS NDVI does not appear after download**: your QGIS build's GDAL lacks the HDF4 driver. The .hdf file is still saved in the output folder.
- **Search finds nothing**: widen the dates, raise the cloud limit, or zoom out. The OPERA products only cover recent years (DSWx from 2023, DIST-ALERT from 2022).
- Details of anything skipped or failed are in **View > Panels > Log Messages**, tab *EarthStream*.

## Testing notes

The engine and UI were tested in headless QGIS 3.34 (GDAL 3.8) against synthetic rasters shaped like the real HLS, DSWx, ECOSTRESS and NASADEM files, including cloud-masked medians, change detection, water frequency, polygon areas, clipped downloads, NASADEM read from inside its zip, hillshade lighting direction, and a two-host check that the token is sent only to NASA hosts and removed on the redirect to S3. NASA's live servers were not reachable from the build machine, so the first real search and stream should be treated as the live check.
