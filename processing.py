# Raster work for EarthStream: streaming paths, analysis grids, cloud masking,
# spectral indices, temporal aggregation, statistics and clipped downloads.
#
# Nothing in here imports QGIS, so it runs inside a QgsTask worker thread and can
# be tested on its own. Everything goes through GDAL (/vsicurl/ for remote COGs)
# and NumPy, both of which ship with QGIS.

import csv
import math
import os
import re
import warnings
from collections import OrderedDict

import numpy as np
from osgeo import gdal, ogr, osr

from .providers import PROVIDERS, products_for

NODATA_OUT = -9999.0
MAX_PIXELS = 25_000_000          # per analysis grid, about 5000 x 5000
MAX_STACK_VALUES = 1_200_000_000  # scenes x pixels kept on disk for aggregation (~4.8 GB)


class Canceled(Exception):
    pass


# ----- paths -------------------------------------------------------------------------

def vsi_path(cfg, href):
    """GDAL path that streams a remote file instead of downloading it."""
    if cfg.get("zipped"):
        name = href.rsplit("/", 1)[-1]
        inner = re.sub(r"^NASADEM_HGT_", "", name, flags=re.I)
        inner = inner[:-4] + ".hgt" if inner.lower().endswith(".zip") else inner
        return "/vsizip//vsicurl/%s/%s" % (href, inner)
    return "/vsicurl/" + href


def local_zip_path(zip_path):
    name = os.path.basename(zip_path)
    inner = re.sub(r"^NASADEM_HGT_", "", name, flags=re.I)
    inner = inner[:-4] + ".hgt" if inner.lower().endswith(".zip") else inner
    return "/vsizip/%s/%s" % (zip_path, inner)


def open_raster(path):
    ds = gdal.Open(path)
    if ds is None and path.startswith("/vsizip/"):
        # Fall back to listing the archive if the inner file name differs from what we guessed.
        archive = path.rsplit("/", 1)[0]
        for name in gdal.ReadDir(archive) or []:
            if name.lower().endswith(".hgt"):
                ds = gdal.Open("%s/%s" % (archive, name))
                break
    if ds is None:
        msg = gdal.GetLastErrorMsg() or "unknown error"
        if "401" in msg or "403" in msg or "HTTP response code: 3" in msg:
            msg += " (check that you are signed in to Earthdata)"
        raise RuntimeError("Could not open %s: %s" % (path.split("/")[-1], msg))
    return ds


def resolve_path(path):
    ds = open_raster(path)
    real = ds.GetDescription() or path
    ds = None
    return real


def scene_path(cfg, scene, key):
    href = scene["files"].get(key)
    return vsi_path(cfg, href) if href else None


# ----- grids -------------------------------------------------------------------------

class Grid:
    def __init__(self, wkt, xmin, ymax, res, width, height):
        self.wkt, self.xmin, self.ymax, self.res = wkt, xmin, ymax, res
        self.width, self.height = width, height

    @property
    def xmax(self):
        return self.xmin + self.width * self.res

    @property
    def ymin(self):
        return self.ymax - self.height * self.res

    @property
    def geotransform(self):
        return (self.xmin, self.res, 0.0, self.ymax, 0.0, -self.res)

    @property
    def pixels(self):
        return self.width * self.height


def _srs(wkt_or_epsg):
    srs = osr.SpatialReference()
    if isinstance(wkt_or_epsg, int):
        srs.ImportFromEPSG(wkt_or_epsg)
    else:
        srs.ImportFromWkt(wkt_or_epsg)
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    return srs


def utm_epsg(lon, lat):
    zone = int((lon + 180) // 6) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def transform_bbox(bbox, dst_wkt, densify=21):
    """Transform a lon/lat bbox to dst, densifying the edges so curved edges are covered."""
    w, s, e, n = bbox
    ct = osr.CoordinateTransformation(_srs(4326), _srs(dst_wkt))
    xs, ys = [], []
    for i in range(densify):
        t = i / (densify - 1)
        for lon, lat in ((w + (e - w) * t, s), (w + (e - w) * t, n), (w, s + (n - s) * t), (e, s + (n - s) * t)):
            x, y = ct.TransformPoint(lon, lat)[:2]
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def make_grid(bbox, ref_path, res, max_pixels=MAX_PIXELS):
    """Analysis grid in the reference file's projected CRS (or the local UTM zone)."""
    notes = []
    wkt = ""
    if ref_path:
        ds = open_raster(ref_path)
        wkt = ds.GetProjection()
        ds = None
    srs = _srs(wkt) if wkt else None
    if srs is None or srs.IsGeographic():
        lon = (bbox[0] + bbox[2]) / 2.0
        lat = (bbox[1] + bbox[3]) / 2.0
        epsg = utm_epsg(lon, lat)
        wkt = _srs(epsg).ExportToWkt()
        notes.append("Working in UTM (EPSG:%d) so distances and slopes are in metres." % epsg)

    minx, miny, maxx, maxy = transform_bbox(bbox, wkt)
    res = float(res)
    while True:
        x0 = math.floor(minx / res) * res
        y1 = math.ceil(maxy / res) * res
        width = max(1, int(math.ceil((maxx - x0) / res)))
        height = max(1, int(math.ceil((y1 - miny) / res)))
        if width * height <= max_pixels:
            break
        res = math.ceil(res * math.sqrt(width * height / max_pixels))
    return Grid(wkt, x0, y1, res, width, height), notes


def coarsen_for_stack(grid, n_layers, bbox, ref_path):
    if grid.pixels * n_layers <= MAX_STACK_VALUES:
        return grid, None
    target = MAX_STACK_VALUES // max(1, n_layers)
    new, _ = make_grid(bbox, ref_path, grid.res, max_pixels=target)
    return new, ("Resolution raised to %g m so %d observations fit in the working space." % (new.res, n_layers))


# ----- reading -----------------------------------------------------------------------

def _gdal_callback(is_canceled):
    if not is_canceled:
        return None

    def cb(_complete, _msg, _data):
        return 0 if is_canceled() else 1
    return cb


def warp_to_grid(sources, grid, resample="bilinear", src_nodata=None, is_canceled=None):
    """Warp one or more rasters onto the grid, returning float32 with NaN as nodata."""
    if isinstance(sources, str):
        sources = [sources]
    # Resolve zipped tiles to the .hgt actually inside, so a wrong guess gives a clear error.
    sources = [resolve_path(p) if p.startswith("/vsizip/") else p for p in sources]
    opts = gdal.WarpOptions(
        format="MEM",
        outputBounds=(grid.xmin, grid.ymin, grid.xmax, grid.ymax),
        width=grid.width,
        height=grid.height,
        dstSRS=grid.wkt,
        outputType=gdal.GDT_Float32,
        srcNodata=src_nodata,
        dstNodata=float("nan"),
        resampleAlg=resample,
        multithread=True,
        warpOptions=["NUM_THREADS=ALL_CPUS"],
        callback=_gdal_callback(is_canceled),
    )
    ds = gdal.Warp("", sources, options=opts)
    if is_canceled and is_canceled():
        raise Canceled()
    if ds is None:
        raise RuntimeError("Could not read %s: %s" % (sources[0].split("/")[-1],
                                                     gdal.GetLastErrorMsg() or "warp failed"))
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ds = None
    return arr


def _resample(grid, cfg, categorical):
    coarser = grid.res > cfg["native_res"] * 1.5
    if categorical:
        return "mode" if coarser else "near"
    return "average" if coarser else "bilinear"


# ----- masks and formulas --------------------------------------------------------------

def hls_bad_pixels(fmask, clouds=True, snow=False, water=False):
    """HLS v2 Fmask bits: 0 cirrus, 1 cloud, 2 adjacent to cloud/shadow, 3 cloud shadow, 4 snow/ice, 5 water."""
    valid = np.isfinite(fmask)
    bits = np.where(valid, fmask, 0).astype(np.uint16)
    bad = np.zeros(fmask.shape, dtype=bool)
    if clouds:
        bad |= (bits & 0b1111) != 0
    if snow:
        bad |= (bits & (1 << 4)) != 0
    if water:
        bad |= (bits & (1 << 5)) != 0
    return bad | ~valid


def _nd(a, b):
    return (a - b) / (a + b)


FORMULAS = {
    "NDVI": lambda b: _nd(b["nir"], b["red"]),
    "EVI": lambda b: 2.5 * (b["nir"] - b["red"]) / (b["nir"] + 6.0 * b["red"] - 7.5 * b["blue"] + 1.0),
    "SAVI": lambda b: 1.5 * (b["nir"] - b["red"]) / (b["nir"] + b["red"] + 0.5),
    "NDWI": lambda b: _nd(b["green"], b["nir"]),
    "MNDWI": lambda b: _nd(b["green"], b["swir1"]),
    "NDMI": lambda b: _nd(b["nir"], b["swir1"]),
    "NBR": lambda b: _nd(b["nir"], b["swir2"]),
    "NDBI": lambda b: _nd(b["swir1"], b["nir"]),
}


def compute_product(cfg, product, spec, scene, grid, options, is_canceled=None):
    """One scene's product on the grid as float32 with NaN where there is no clear data."""
    kind = cfg["kind"]
    options = options or {}
    with np.errstate(divide="ignore", invalid="ignore"):
        if kind == "reflectance":
            bands = {}
            for key in spec["inputs"]:
                raw = warp_to_grid(scene_path(cfg, scene, key), grid, _resample(grid, cfg, False),
                                   cfg["nodata"], is_canceled)
                bands[key] = raw * cfg["scale"]
            if any(options.get(k) for k in ("mask_clouds", "mask_snow", "mask_water")) and "fmask" in scene["files"]:
                fm = warp_to_grid(scene_path(cfg, scene, "fmask"), grid, "near", 255, is_canceled)
                bad = hls_bad_pixels(fm, options.get("mask_clouds", True), options.get("mask_snow", False),
                                     options.get("mask_water", False))
                for k in bands:
                    bands[k][bad] = np.nan
            if product in FORMULAS:
                out = FORMULAS[product](bands)
                out = np.clip(out, -1.0, 1.0)
            else:
                out = bands[spec["inputs"][0]]
                out[(out < -0.05) | (out > 1.6)] = np.nan

        elif kind == "thematic" and "wtr" in spec["inputs"]:
            # 0 not water, 1 open water, 2 partial surface water; 252 snow/ice, 253 cloud,
            # 254 ocean masked, 255 fill. Water counts as 100 so the temporal mean is a percentage.
            wtr = warp_to_grid(scene_path(cfg, scene, "wtr"), grid, _resample(grid, cfg, True), 255, is_canceled)
            out = np.full(wtr.shape, np.nan, dtype=np.float32)
            out[wtr == 0] = 0.0
            out[(wtr == 1) | (wtr == 2)] = 100.0

        elif kind == "thematic":
            key = spec["inputs"][0]
            out = warp_to_grid(scene_path(cfg, scene, key), grid, _resample(grid, cfg, False), 255, is_canceled)
            out[(out < 0) | (out > 100)] = np.nan

        elif kind == "lst":
            key = spec["inputs"][0]
            out = warp_to_grid(scene_path(cfg, scene, key), grid, _resample(grid, cfg, False), None, is_canceled)
            if key == "lst":
                out[(out <= 150) | (out >= 400)] = np.nan
                out = out - 273.15
            else:
                out[(out <= 0) | (out > 1.0)] = np.nan
            if options.get("mask_clouds", True) and "cloud" in scene["files"]:
                cl = warp_to_grid(scene_path(cfg, scene, "cloud"), grid, "near", 255, is_canceled)
                out[cl == 1] = np.nan
        else:
            raise RuntimeError("%s can't be analyzed while streaming." % cfg["short_name"])
    out = out.astype(np.float32)
    out[~np.isfinite(out)] = np.nan
    return out


def dem_product(cfg, product, scenes, grid, is_canceled=None):
    """Mosaic every selected DEM tile onto the grid, then derive slope or hillshade."""
    sources = [scene_path(cfg, sc, "dem") for sc in scenes if "dem" in sc["files"]]
    if not sources:
        raise RuntimeError("None of the selected tiles has a DEM layer.")
    elev = warp_to_grid(sources, grid, "bilinear", cfg["nodata"], is_canceled)
    elev[(elev < -1000) | (elev > 9000)] = np.nan
    if product.startswith("Elevation"):
        return elev
    with np.errstate(invalid="ignore"):
        dz_drow, dz_dcol = np.gradient(elev, grid.res)
        dzdx, dzdy = dz_dcol, -dz_drow          # east and north
        if product.startswith("Slope"):
            return np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)
        # Hillshade: dot product of the surface normal with a light from 315 deg, 45 deg up.
        az, alt = math.radians(315.0), math.radians(45.0)
        lx, ly, lz = math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)
        norm = np.sqrt(dzdx * dzdx + dzdy * dzdy + 1.0)
        shade = (-dzdx * lx - dzdy * ly + lz) / norm
        return (np.clip(shade, 0, 1) * 255.0).astype(np.float32)


# ----- area of interest ---------------------------------------------------------------

def rasterize_aoi(geojson_path, grid):
    """Boolean mask of grid pixels inside the AOI polygons (GeoJSON in EPSG:4326)."""
    src = ogr.Open(geojson_path)
    if src is None:
        return None
    layer = src.GetLayer(0)
    dst_srs = _srs(grid.wkt)
    ct = osr.CoordinateTransformation(_srs(4326), dst_srs)

    mem = ogr.GetDriverByName("Memory").CreateDataSource("aoi")
    mlayer = mem.CreateLayer("aoi", dst_srs, ogr.wkbMultiPolygon)
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        geom = geom.Clone()
        geom.Transform(ct)
        f = ogr.Feature(mlayer.GetLayerDefn())
        f.SetGeometry(geom)
        mlayer.CreateFeature(f)

    ds = gdal.GetDriverByName("MEM").Create("", grid.width, grid.height, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(grid.geotransform)
    ds.SetProjection(grid.wkt)
    gdal.RasterizeLayer(ds, [1], mlayer, burn_values=[1])
    mask = ds.GetRasterBand(1).ReadAsArray().astype(bool)
    return mask if mask.any() else None


# ----- statistics and aggregation -------------------------------------------------------

def stats(arr, area_pixels):
    vals = arr[np.isfinite(arr)]
    if vals.size == 0:
        return dict(valid_pct=0.0, mean=None, median=None, std=None, min=None, max=None, p10=None, p90=None)
    p10, med, p90 = np.percentile(vals, [10, 50, 90])
    return dict(
        valid_pct=round(100.0 * vals.size / max(1, area_pixels), 2),
        mean=float(vals.mean()), median=float(med), std=float(vals.std()),
        min=float(vals.min()), max=float(vals.max()), p10=float(p10), p90=float(p90),
    )


def aggregate(stack, method, is_canceled=None):
    """Reduce a (time, rows, cols) stack, a block of rows at a time to keep memory flat."""
    n, h, w = stack.shape
    out = np.full((h, w), np.nan, dtype=np.float32)
    rows = max(16, int(64_000_000 / max(1, n * w * 4)))
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for y in range(0, h, rows):
            if is_canceled and is_canceled():
                raise Canceled()
            block = np.asarray(stack[:, y:y + rows, :])
            if method == "Median":
                res = np.nanmedian(block, axis=0)
            elif method == "Mean":
                res = np.nanmean(block, axis=0)
            elif method == "Maximum":
                res = np.nanmax(block, axis=0)
            elif method == "Minimum":
                res = np.nanmin(block, axis=0)
            elif method == "Std. deviation":
                res = np.nanstd(block, axis=0)
                res[np.sum(np.isfinite(block), axis=0) < 2] = np.nan
            elif method == "Valid count":
                res = np.sum(np.isfinite(block), axis=0).astype(np.float32)
            elif method.startswith("Change"):
                # Layers are in date order: last clear value minus first clear value.
                finite = np.isfinite(block)
                count = finite.sum(axis=0)
                first_idx = np.argmax(finite, axis=0)
                last_idx = n - 1 - np.argmax(finite[::-1], axis=0)
                first = np.take_along_axis(block, first_idx[None], axis=0)[0]
                last = np.take_along_axis(block, last_idx[None], axis=0)[0]
                res = last - first
                res[count < 2] = np.nan
            else:
                raise ValueError("Unknown method %s" % method)
            out[y:y + rows, :] = res
    return out


def write_geotiff(path, arr, grid, description="", metadata=None):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, grid.width, grid.height, 1, gdal.GDT_Float32,
                    ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "BIGTIFF=IF_SAFER"])
    if ds is None:
        raise RuntimeError("Could not create %s: %s" % (path, gdal.GetLastErrorMsg()))
    ds.SetGeoTransform(grid.geotransform)
    ds.SetProjection(grid.wkt)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA_OUT)
    if description:
        band.SetDescription(description)
    data = np.where(np.isfinite(arr), arr, NODATA_OUT).astype(np.float32)
    band.WriteArray(data)
    band.ComputeStatistics(False)
    if metadata:
        ds.SetMetadata({k: str(v) for k, v in metadata.items()})
    ds.FlushCache()
    ds = None
    return path


# ----- the analysis run ------------------------------------------------------------------

def group_by_date(scenes):
    """Granules from the same day and sensor become one observation (adjacent tiles)."""
    groups = OrderedDict()
    for sc in sorted(scenes, key=lambda s: s["datetime"]):
        key = (sc["datetime"][:10], sc["sensor"])
        groups.setdefault(key, []).append(sc)
    return groups


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def analyze(provider_name, product, scenes, bbox, res, method, options, out_dir, aoi_geojson=None,
            want_composite=True, want_series=True, progress=None, status=None, is_canceled=None):
    """Aggregate and/or summarise the selected scenes. Returns a dict describing the outputs."""
    cfg = PROVIDERS[provider_name]
    spec = products_for(provider_name)[product]
    if not cfg["streamable"]:
        raise RuntimeError(cfg.get("stream_message", "This platform can't be analyzed while streaming."))
    if not scenes:
        raise RuntimeError("Select at least one scene in the Imagery Footprint table.")

    progress = progress or (lambda v: None)
    status = status or (lambda t: None)
    cancel = is_canceled or (lambda: False)
    os.makedirs(out_dir, exist_ok=True)

    ref_key = spec["inputs"][0]
    ref_scene = next((s for s in scenes if ref_key in s["files"]), None)
    if ref_scene is None:
        raise RuntimeError("None of the selected scenes has the %s layer." % ref_key)

    status("Setting up the analysis grid...")
    grid, notes = make_grid(bbox, scene_path(cfg, ref_scene, ref_key), res)
    if grid.res != float(res):
        notes.append("Resolution raised from %g m to %g m to keep the grid under %d million pixels."
                     % (float(res), grid.res, MAX_PIXELS // 1_000_000))

    aoi = rasterize_aoi(aoi_geojson, grid) if aoi_geojson else None
    area_pixels = int(aoi.sum()) if aoi is not None else grid.pixels

    dates = sorted(s["datetime"][:10] for s in scenes)
    stem = "%s_%s_%s_%s" % (slug(cfg["short_name"]), slug(product), dates[0].replace("-", ""),
                            dates[-1].replace("-", ""))
    result = dict(grid_res=grid.res, notes=notes, product=product, spec=spec, observations=0,
                  composite=None, csv=None, png=None, skipped=[])
    rows = []

    if cfg["kind"] == "dem":
        status("Mosaicking %d DEM tile(s)..." % len(scenes))
        arr = dem_product(cfg, product, scenes, grid, cancel)
        if aoi is not None:
            arr[~aoi] = np.nan
        progress(80)
        result["observations"] = 1
        if want_series:
            st = stats(arr, area_pixels)
            st.update(date="static", sensor=scenes[0]["sensor"], granules=len(scenes))
            rows.append(st)
        if want_composite:
            path = os.path.join(out_dir, "%s.tif" % stem)
            write_geotiff(path, arr, grid, product, dict(source=cfg["short_name"], product=product,
                                                         tiles=len(scenes)))
            result["composite"] = path
    else:
        groups = group_by_date(scenes)
        n = len(groups)
        stack = None
        stack_path = None
        if want_composite and n > 1:
            grid2, note = coarsen_for_stack(grid, n, bbox, scene_path(cfg, ref_scene, ref_key))
            if note:
                notes.append(note)
                grid = grid2
                aoi = rasterize_aoi(aoi_geojson, grid) if aoi_geojson else None
                area_pixels = int(aoi.sum()) if aoi is not None else grid.pixels
            stack_path = os.path.join(out_dir, ".%s_stack.dat" % stem)
            stack = np.lib.format.open_memmap(stack_path, mode="w+", dtype=np.float32,
                                              shape=(n, grid.height, grid.width))
        single = None
        for i, ((day, sensor), members) in enumerate(groups.items()):
            if cancel():
                raise Canceled()
            status("Observation %d of %d: %s %s" % (i + 1, n, day, sensor))
            parts = []
            for sc in members:
                missing = [k for k in spec["inputs"] if k not in sc["files"]]
                if missing:
                    result["skipped"].append("%s (missing %s)" % (sc["id"], ", ".join(missing)))
                    continue
                try:
                    parts.append(compute_product(cfg, product, spec, sc, grid, options, cancel))
                except Canceled:
                    raise
                except Exception as e:  # one bad granule shouldn't sink the run
                    result["skipped"].append("%s (%s)" % (sc["id"], e))
            if parts:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=RuntimeWarning)
                    arr = parts[0] if len(parts) == 1 else np.nanmean(np.stack(parts), axis=0)
            else:
                arr = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
            if aoi is not None:
                arr[~aoi] = np.nan
            if want_series and parts:
                st = stats(arr, area_pixels)
                st.update(date=day, sensor=sensor, granules=len(parts))
                rows.append(st)
            if stack is not None:
                stack[i] = arr
            elif want_composite:
                single = arr
            if parts:
                result["observations"] += 1
            progress(5 + 80.0 * (i + 1) / n)

        if want_composite and result["observations"]:
            path = os.path.join(out_dir, "%s_%s.tif" % (stem, slug(method)))
            if stack is not None:
                status("Aggregating %d observations (%s)..." % (n, method.lower()))
                comp = aggregate(stack, method, cancel)
            else:
                comp = single
                if method in ("Valid count",):
                    comp = np.isfinite(comp).astype(np.float32)
                elif method.startswith("Change") or method == "Std. deviation":
                    comp = np.full_like(comp, np.nan)
                    notes.append("%s needs at least two dates; only one was selected." % method)
            write_geotiff(path, comp, grid, "%s %s" % (product, method.lower()),
                          dict(source=cfg["short_name"], product=product, method=method,
                               observations=result["observations"], start=dates[0], end=dates[-1]))
            result["composite"] = path
            result["method"] = method
        if stack is not None:
            del stack
            try:
                os.remove(stack_path)
            except OSError:
                pass

    if want_series and rows:
        result["csv"] = write_series_csv(os.path.join(out_dir, "%s_timeseries.csv" % stem), rows)
        result["png"] = plot_series(os.path.join(out_dir, "%s_timeseries.png" % stem), rows, product, spec)
        result["rows"] = rows
    progress(100)
    return result


SERIES_FIELDS = ["date", "sensor", "granules", "valid_pct", "mean", "median", "std", "min", "max", "p10", "p90"]


def write_series_csv(path, rows):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SERIES_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else (round(r[k], 5) if isinstance(r[k], float) else r[k]))
                             for k in SERIES_FIELDS})
    return path


def plot_series(path, rows, product, spec):
    """A quick PNG chart if matplotlib is available (it ships with most QGIS installs)."""
    pts = [r for r in rows if r.get("mean") is not None and r["date"] != "static"]
    if len(pts) < 2:
        return None
    try:
        import datetime as dt
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.figure import Figure
    except Exception:
        return None
    dates = [dt.datetime.strptime(r["date"], "%Y-%m-%d") for r in pts]
    fig = Figure(figsize=(8, 3.6), dpi=120)
    ax = fig.add_subplot(111)
    ax.fill_between(dates, [r["p10"] for r in pts], [r["p90"] for r in pts], color="#9ecae1", alpha=0.5,
                    linewidth=0, label="10th to 90th percentile")
    ax.plot(dates, [r["mean"] for r in pts], color="#08519c", marker="o", markersize=3, linewidth=1.4,
            label="Mean")
    ax.set_title(product)
    ax.set_ylabel(spec.get("units") or "")
    ax.grid(alpha=0.3)
    ax.legend(loc="best", fontsize=8, frameon=False)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close("all")
    return path


# ----- loading and downloading helpers ----------------------------------------------------

def build_stack_vrt(paths, out_path, descriptions=None, nodata=None):
    opts = gdal.BuildVRTOptions(separate=True, srcNodata=nodata, VRTNodata=nodata)
    ds = gdal.BuildVRT(out_path, paths, options=opts)
    if ds is None:
        raise RuntimeError("Could not build %s: %s" % (os.path.basename(out_path), gdal.GetLastErrorMsg()))
    for i, name in enumerate(descriptions or [], start=1):
        ds.GetRasterBand(i).SetDescription(name)
    ds.FlushCache()
    ds = None
    return out_path


def clip_file(src_path, bbox, out_path, is_canceled=None):
    """Cut a remote raster to the bbox in its own CRS and resolution, reading only what's needed."""
    ds = open_raster(src_path)
    wkt = ds.GetProjection()
    gt = ds.GetGeoTransform()
    minx, miny, maxx, maxy = transform_bbox(bbox, wkt)
    # Stay inside the raster so partial overlaps don't fail.
    rx0, ry1 = gt[0], gt[3]
    rx1, ry0 = gt[0] + gt[1] * ds.RasterXSize, gt[3] + gt[5] * ds.RasterYSize
    minx, maxx = max(minx, rx0), min(maxx, rx1)
    miny, maxy = max(miny, ry0), min(maxy, ry1)
    if minx >= maxx or miny >= maxy:
        return None
    opts = gdal.TranslateOptions(projWin=[minx, maxy, maxx, miny],
                                 creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
                                 callback=_gdal_callback(is_canceled))
    out = gdal.Translate(out_path, ds, options=opts)
    if is_canceled and is_canceled():
        raise Canceled()
    if out is None:
        raise RuntimeError("Could not clip %s: %s" % (os.path.basename(out_path), gdal.GetLastErrorMsg()))
    out = None
    return out_path


def modis_ndvi_subdataset(hdf_path):
    if gdal.GetDriverByName("HDF4") is None:
        return None
    return 'HDF4_EOS:EOS_GRID:"%s":MODIS_Grid_16DAY_250m_500m_VI:"250m 16 days NDVI"' % hdf_path
