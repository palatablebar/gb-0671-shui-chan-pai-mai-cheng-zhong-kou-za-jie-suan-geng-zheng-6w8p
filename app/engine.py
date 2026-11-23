from importlib import import_module
from copy import deepcopy
import json, time, base64, hashlib
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest
from app.modules.recall.evidence import EVIDENCE_FILES

RECALL_SAMPLE_FILES = {
    'inspections': 'recall_inspections.csv',
    'sales': 'recall_sales.csv',
    'destinations': 'recall_destinations.csv',
}


def _load_recall_raws(payload, snapshot):
    """召回证据读取。默认走样例文件；payload/快照可整体覆盖，原始字节全程不丢。"""
    if snapshot is not None and 'recall_raw_b64' in snapshot:
        return {k: base64.b64decode(snapshot['recall_raw_b64'][k], validate=True)
                for k in EVIDENCE_FILES}
    overrides = payload.get('recall_evidence', {})
    if not isinstance(overrides, dict):
        raise BusinessError('BAD_CONTEXT', '召回证据参数须为对象')
    raws = {}
    for k in EVIDENCE_FILES:
        if k in overrides:
            if not isinstance(overrides[k], str):
                raise BusinessError('BAD_CONTEXT', f'{k} 证据须为 CSV 文本')
            raws[k] = overrides[k].encode()
        else:
            raws[k] = (ROOT / 'sample_data' / RECALL_SAMPLE_FILES[k]).read_bytes()
        if len(raws[k]) > MAX_BYTES:
            raise BusinessError('TOO_LARGE', f'{k} 证据超过2MB', 413)
    return raws


def evaluate(payload=None, until=None, snapshot=None):
    payload = payload or {}
    until = len(MODULES) if until is None else until
    dataset = payload.get('dataset', 'dirty')
    if dataset not in {'dirty', 'clean', 'legacy'}: raise BusinessError('BAD_DATASET', '样例名称无效')
    if not isinstance(payload.get('overrides', {}), dict): raise BusinessError('BAD_CONTEXT', '参数须为对象')
    raw = (base64.b64decode(snapshot['bytes_b64'], validate=True) if snapshot is not None else (str(payload['csv_text']).encode() if 'csv_text' in payload else (ROOT / 'sample_data' / f'{dataset}.csv').read_bytes()))
    if len(raw) > MAX_BYTES: raise BusinessError('TOO_LARGE', '文件超过2MB', 413)
    recall_raws = _load_recall_raws(payload, snapshot)
    context = deepcopy(snapshot['context']) if snapshot is not None else json.loads((ROOT / 'sample_data' / 'context.json').read_text())
    context.update(deepcopy(payload.get('overrides', {})))
    start = time.perf_counter()
    state = {'raw_bytes': raw, 'context': context, 'records': [], 'quarantine': [], 'counts': {}, 'views': {}, 'source': dataset}
    recall_digest = hashlib.sha256(b''.join(recall_raws[k] for k in EVIDENCE_FILES)).hexdigest()
    for index, key in enumerate(MODULES, 1):
        if index > until: break
        if key == 'recall':
            state['recall_raw'] = recall_raws
            state['recall_digest'] = recall_digest
        import_module(f'app.modules.{key}.rules').apply(state)
    state['input_snapshot'] = {
        'bytes_b64': base64.b64encode(raw).decode(),
        'context': deepcopy(context),
        'recall_raw_b64': {k: base64.b64encode(v).decode() for k, v in recall_raws.items()},
        'recall_digest': recall_digest,
    }
    state.pop('raw_bytes', None)
    state.pop('recall_raw', None)
    state['manifest'] = make_manifest(digest(raw, context, recall_digest), dataset, time.perf_counter() - start)
    return state
