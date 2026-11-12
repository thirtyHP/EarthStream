# Background work. Each job runs in a QgsTask so the QGIS window stays responsive,
# and hands a plain result back to the dock on the main thread. Layers are never
# created here: the work functions return "layer specs" (path, name, style) and the
# dock turns those into map layers once the task finishes.

import os
import re
import traceback

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask

from . import cmr_client, credentials, processing
from .providers import PROVIDERS
from .qt_compat import task_can_cancel


class EarthTask(QgsTask):
    statusChanged = pyqtSignal(str)

    def __init__(self, description, fn, on_done, on_error):
        super().__init__(description, task_can_cancel())
        self.fn = fn
        self.on_done = on_done
        self.on_error = on_error
        self.result = None
        self.error = None
        self.trace = ""

    def run(self):
        try:
            self.result = self.fn(self)
            return True
        except (processing.Canceled, InterruptedError):
            return False
        except Exception as e:
            self.error = e
            self.trace = traceback.format_exc()
            return False

    def finished(self, ok):
        if ok:
            self.on_done(self.result)
        else:
            self.on_error(self.error, self.isCanceled(), self.trace)

    # helpers the work functions call from the worker thread
    def status(self, text):
        self.statusChanged.emit(text)

    def canceled(self):
        return self.isCanceled()


# ----- sign in ------------------------------------------------------------------------

def signin_work(username, password):
    def work(task):
        task.status("Signing in to Earthdata Login...")
        return credentials.fetch_token(username, password)
    return work


# ----- search -------------------------------------------------------------------------

def search_work(provider_name, bbox, start, end, max_cloud, limit):
    def work(task):
        return cmr_client.search(provider_name, bbox, start, end, max_cloud, limit,
                                 is_canceled=task.canceled, status=task.status)
    return work


# ----- load visual / multispectral -------------------------------------------------------

def _single_band_spec(cfg, path, name, key):
    style = cfg.get("visual_style")
    if style == "palette":
        return dict(path=path, name=name, style="palette")
    if style == "hillshade":
        return dict(path=path, name=name, style="hillshade", geographic=True)
    if style == "lst":
        return dict(path=path, name=name, style="pseudo", ramp="Spectral", invert=True,
                    range=cfg.get("visual_range"))
    return dict(path=path, name=name, style="pseudo", ramp="Greys", invert=False, range=None)


def load_work(provider_name, scenes, mode, vrt_dir):
    """mode is 'visual' or 'ms'. Returns a list of layer specs."""
    cfg = PROVIDERS[provider_name]

    def work(task):
        os.makedirs(vrt_dir, exist_ok=True)
        specs, problems = [], []
        for i, sc in enumerate(scenes):
            if task.canceled():
                break
            task.status("Opening %s (%d of %d)..." % (sc["id"], i + 1, len(scenes)))
            try:
                if mode == "visual":
                    keys = list(cfg["visual"])
                    label = "Visual"
                else:
                    keys = list(cfg["ms_bands"])
                    label = "Multispectral"
                missing = [k for k in keys if k not in sc["files"]]
                if missing:
                    problems.append("%s is missing %s" % (sc["id"], ", ".join(missing)))
                    continue
                paths = [processing.scene_path(cfg, sc, k) for k in keys]
                name = "%s %s %s" % (sc["datetime"][:10], sc["tile"] or sc["id"], label)
                if len(keys) == 1:
                    path = processing.resolve_path(paths[0])  # also proves we can read it
                    specs.append(_single_band_spec(cfg, path, name, keys[0]))
                else:
                    vrt = os.path.join(vrt_dir, "%s_%s.vrt" % (_safe(sc["id"]), mode))
                    processing.build_stack_vrt(paths, vrt, descriptions=keys, nodata=cfg["nodata"])
                    if mode == "visual":
                        bands = [1, 2, 3]
                    else:
                        bands = [keys.index(k) + 1 for k in cfg["ms_default"]]
                    specs.append(dict(path=vrt, name=name, style="rgb", bands=bands,
                                      range=cfg.get("visual_range", (0, 3000)),
                                      ms_range=(0, 4000) if mode == "ms" else None))
            except Exception as e:
                problems.append("%s: %s" % (sc["id"], e))
            task.setProgress(100.0 * (i + 1) / len(scenes))
        if not specs and problems:
            raise RuntimeError(problems[0])
        return dict(specs=specs, problems=problems)
    return work


# ----- download ----------------------------------------------------------------------

def download_work(provider_name, scenes, bbox, clip, out_dir):
    cfg = PROVIDERS[provider_name]

    def work(task):
        os.makedirs(out_dir, exist_ok=True)
        specs, problems, written = [], [], []
        n = len(scenes)
        for i, sc in enumerate(scenes):
            if task.canceled():
                break
            folder = os.path.join(out_dir, _safe(sc["id"]))
            os.makedirs(folder, exist_ok=True)
            links = [u for u in sc["links"] if re.search(cfg["download_regex"], u.rsplit("/", 1)[-1])]
            if not links:
                problems.append("%s has no files to download" % sc["id"])
                continue
            local = {}
            base = 100.0 * i / n
            for j, url in enumerate(links):
                if task.canceled():
                    break
                fname = url.rsplit("/", 1)[-1]
                task.status("%s: %s (%d of %d)" % (sc["id"], fname, j + 1, len(links)))
                try:
                    if clip and cfg["streamable"]:
                        out = os.path.join(folder, re.sub(r"\.(zip|hgt)$", ".tif", fname, flags=re.I))
                        path = processing.clip_file(processing.vsi_path(cfg, url), bbox, out, task.canceled)
                        if path is None:
                            continue
                    else:
                        out = os.path.join(folder, fname)

                        def prog(done, total, _j=j):
                            if total:
                                frac = (_j + done / float(total)) / len(links)
                                task.setProgress(base + 100.0 * frac / n)
                        path = credentials.download(url, out, prog, task.canceled)
                    written.append(path)
                    for key, pattern in cfg["bands"].items():
                        if re.search(pattern, fname):
                            local.setdefault(key, path)
                except (processing.Canceled, InterruptedError):
                    raise
                except Exception as e:
                    problems.append("%s: %s" % (fname, e))
                task.setProgress(base + 100.0 * (j + 1) / len(links) / n)

            spec = _local_layer_spec(cfg, sc, local, folder, clip)
            if spec:
                specs.append(spec)
        if not written and problems:
            raise RuntimeError(problems[0])
        return dict(specs=specs, problems=problems, written=written, out_dir=out_dir)
    return work


def _local_layer_spec(cfg, sc, local, folder, clip):
    name = "%s %s (downloaded)" % (sc["datetime"][:10], sc["tile"] or sc["id"])
    try:
        if cfg["kind"] == "archive":
            sub = processing.modis_ndvi_subdataset(local.get("hdf", ""))
            if sub:
                return dict(path=sub, name=name + " NDVI", style="pseudo", ramp="RdYlGn", invert=False,
                            range=(-2000, 9000))
            return None
        if cfg["kind"] == "reflectance":
            keys = [k for k in cfg["ms_bands"] if k in local]
            if len(keys) < 3:
                return None
            vrt = os.path.join(folder, "%s_stack.vrt" % _safe(sc["id"]))
            processing.build_stack_vrt([local[k] for k in keys], vrt, descriptions=keys, nodata=cfg["nodata"])
            want = [k for k in cfg["visual"] if k in keys]
            bands = [keys.index(k) + 1 for k in want] if len(want) == 3 else [1, 2, 3]
            return dict(path=vrt, name=name, style="rgb", bands=bands, range=cfg.get("visual_range", (0, 3000)))
        key = cfg["visual"][0] if cfg.get("visual") else None
        path = local.get(key)
        if not path:
            return None
        if cfg.get("zipped") and not clip:
            path = processing.resolve_path(processing.local_zip_path(path))
        return _single_band_spec(cfg, path, name, key)
    except Exception:
        return None


# ----- analysis ------------------------------------------------------------------------

def analyze_work(provider_name, product, scenes, bbox, res, method, options, out_dir, aoi_geojson,
                 want_composite, want_series):
    def work(task):
        return processing.analyze(
            provider_name, product, scenes, bbox, res, method, options, out_dir, aoi_geojson,
            want_composite, want_series, progress=task.setProgress, status=task.status,
            is_canceled=task.canceled)
    return work


def _safe(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:120]
