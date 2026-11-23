"""Bounded native Render View operations; executed on Houdini's main thread."""
import math
import os
import uuid

import hou


def _viewer(name, create=False):
    if not hou.isUIAvailable():
        raise ValueError('Render View requires a graphical Houdini session')
    matches = [p for p in hou.ui.paneTabs() if p.name() == name]
    if matches:
        viewer = matches[0]
        if viewer.type() != hou.paneTabType.IPRViewer:
            raise ValueError('Named pane is not a Render View')
        return viewer
    if not create:
        raise ValueError('Named Render View does not exist')
    viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
    viewer.setName(name)
    return viewer


def _records():
    if not hasattr(hou.session, '_fx_render_view_records'):
        hou.session._fx_render_view_records = {}
    return hou.session._fx_render_view_records


def start(node_path, pane_name='FX_Render_View', frame=None, preview=False):
    rop = hou.node(node_path)
    if rop is None or not isinstance(rop, hou.RopNode):
        raise ValueError('Expected an existing render output node')
    viewer = _viewer(pane_name, create=True)
    if viewer.isRendering():
        raise ValueError('Render View is busy; explicitly stop it before replacing its render')
    if frame is not None:
        if not math.isfinite(frame):
            raise ValueError('Frame must be finite')
        hou.setFrame(frame)
    viewer.setAutoUpdate(False)
    viewer.setPreview(preview)
    viewer.setRopNode(rop)
    record = {'token': uuid.uuid4().hex, 'rop': rop.path(), 'frame': hou.frame()}
    viewer.startRender()
    _records()[pane_name] = record
    return {'state': 'requested', 'pane': pane_name, 'preview': viewer.isPreviewOn(), **record}


def status(pane_name='FX_Render_View'):
    viewer = _viewer(pane_name)
    rop = viewer.ropNode()
    record = _records().get(pane_name, {})
    result = {'pane': pane_name, 'rendering': viewer.isRendering(), 'active': viewer.isActive(),
              'rop': rop.path() if rop else None, 'current_frame': hou.frame(), **record}
    try:
        result['resolution'] = list(viewer.imageResolution())
        result['planes'] = list(viewer.planes())
    except hou.OperationFailed:
        result['resolution'] = None
        result['planes'] = []
    return result


def save(output_path, pane_name='FX_Render_View', token=None, overwrite=False):
    viewer = _viewer(pane_name)
    state = status(pane_name)
    if viewer.isRendering():
        raise ValueError('Render is still running; an incomplete image is not a completed check')
    if not state.get('token') or (token is not None and state['token'] != token):
        raise ValueError('Missing or stale render token')
    rop = viewer.ropNode()
    if rop is None or rop.path() != state['rop'] or hou.frame() != state['frame']:
        raise ValueError('ROP or frame changed since the render request')
    if not state['resolution'] or not all(state['resolution']):
        raise ValueError('Render View has no image')
    path = hou.expandString(output_path)
    if not os.path.isabs(path) or os.path.splitext(path)[1].lower() not in ('.exr', '.png'):
        raise ValueError('Use an absolute or $HIP-relative EXR/PNG output path')
    if os.path.exists(path) and not overwrite:
        raise ValueError('Output exists; choose a new check name or explicitly overwrite')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pixels = viewer.pixels('C')
    if not pixels:
        raise ValueError('Render View has no color pixels')
    total_pixels = len(pixels)
    # Use vectorized numpy inspection when available, or bounded strided sampling on
    # very large frames so pure Python never freezes Houdini's main thread.
    sampled = False
    try:
        import numpy as np  # type: ignore[import-not-found]

        arr = np.asarray(pixels, dtype=np.float64)
        if arr.ndim == 2 and arr.shape[1] >= 1:
            if arr.shape[1] < 4:
                pad = np.zeros((arr.shape[0], 4 - arr.shape[1]), dtype=np.float64)
                arr = np.hstack((arr, pad))
            else:
                arr = arr[:, :4]
            finite_mask = np.isfinite(arr)
            invalid = int(np.sum(~np.all(finite_mask, axis=1)))
            safe = np.where(finite_mask, arr, 0.0)
            maxima = [
                float(np.max(safe[:, c])) if np.any(finite_mask[:, c]) else 0.0
                for c in range(4)
            ]
            rgb_nonzero = int(np.sum(np.any(safe[:, :3] > 0, axis=1)))
            alpha_nonzero = int(np.sum(safe[:, 3] > 0))
        else:
            raise TypeError('unexpected pixel array shape')
    except Exception:
        stride = max(1, total_pixels // 250000)
        sampled = stride > 1
        maxima_opt = [None, None, None, None]
        alpha_nonzero = 0
        rgb_nonzero = 0
        invalid = 0
        for index in range(0, total_pixels, stride):
            pixel = pixels[index]
            values = list(pixel) + [0.0] * (4 - len(pixel))
            if not all(math.isfinite(v) for v in values):
                invalid += 1
            for component in range(4):
                v = values[component]
                if math.isfinite(v) and (maxima_opt[component] is None or v > maxima_opt[component]):
                    maxima_opt[component] = v
            rgb_nonzero += int(any(math.isfinite(v) and v > 0 for v in values[:3]))
            alpha_nonzero += int(math.isfinite(values[3]) and values[3] > 0)
        maxima = [0.0 if m is None else m for m in maxima_opt]
    saved = viewer.saveFrame(path, 0, -1, -1, 'C', 'C', '*', '', 1.0, False)
    exists = os.path.isfile(path)
    size = os.path.getsize(path) if exists else 0
    return {**state, 'saved': bool(saved and exists and size), 'path': path, 'size_bytes': size,
            'pixel_count': total_pixels, 'pixel_sampled': sampled, 'rgba_max': maxima,
            'rgb_nonzero_pixels': rgb_nonzero, 'alpha_nonzero_pixels': alpha_nonzero,
            'nonfinite_pixels': invalid}


def stop(pane_name='FX_Render_View', token=None):
    viewer = _viewer(pane_name)
    record = _records().get(pane_name, {})
    if token is not None and token != record.get('token'):
        raise ValueError('Stale render token')
    if viewer.isActive() or viewer.isRendering():
        viewer.killRender()
    _records().pop(pane_name, None)
    return {'pane': pane_name, 'rendering': viewer.isRendering(), 'stopped': True}
