"""召回三类原始证据（抽检/成交/去向）的严格读取器。

与称重采信同一套纪律：编码显式归一、表头严格匹配、空行/重复表头计数、
同编号同原值去重、同编号不同原值进隔离清单并保留原始行，绝不静默丢弃证据。
"""
import csv
import io
from app.coerce import number, instant
from app.errors import BusinessError

INSPECTION_FIELDS = ['id', 'batch_no', 'voyage_no', 'fish', 'fail_item',
                     'sampled_at', 'report_version', 'replaces', 'verdict']
SALE_FIELDS = ['id', 'ticket', 'batch_no', 'fish', 'sold_kg',
               'sold_at', 'buyer_id', 'buyer_name']
DESTINATION_FIELDS = ['id', 'ticket', 'buyer_id', 'recalled_kg',
                      'trace_status', 'traced_at', 'note']

EVIDENCE_FILES = ('inspections', 'sales', 'destinations')
VERDICTS = {'不合格', '合格'}
TRACE_STATUS = {'已核清', '失联'}


def _decode(raw):
    text = None
    encoding = None
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise BusinessError('BAD_ENCODING', '证据文件编码无法识别')
    return text, encoding


def load_evidence(raw, fields, kind, quarantine, counters):
    """读取单类证据 CSV，返回去重后的行列表。解析级问题写入 quarantine。"""
    if not raw:
        raise BusinessError('EMPTY_FILE', f'{kind} 证据为空')
    if not raw.strip(b'\xef\xbb\xbf\r\n \t'):
        raise BusinessError('BLANK_FILE', f'{kind} 证据只有空行')
    text, encoding = _decode(raw)
    counters.setdefault(kind, {'input': 0, 'blank': 0, 'repeated_header': 0, 'duplicate': 0})
    stream = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        original = next(stream)
        header = [v.strip() for v in original]
        if header != fields:
            raise BusinessError('BAD_HEADER', f'{kind} 证据字段或顺序不匹配')
        records = []
        for cells in stream:
            line = stream.line_num
            counters[kind]['input'] += 1
            if not cells or not any(v.strip() for v in cells):
                counters[kind]['blank'] += 1
                continue
            if cells == original:
                counters[kind]['repeated_header'] += 1
                continue
            if len(cells) != len(header):
                quarantine.append({'kind': kind, 'line': line, 'id': cells[0] if cells else '',
                                   'code': 'EVIDENCE_BAD_COLUMNS',
                                   'detail': f'{kind} 需要{len(header)}列，实际{len(cells)}列',
                                   'raw': dict(zip(header, cells))})
                continue
            row = dict(zip(header, cells))
            row['_line'] = line
            row['_kind'] = kind
            records.append(row)
    except csv.Error:
        raise BusinessError('BROKEN_CSV', f'{kind} 证据 CSV 引号或末行损坏')
    if not counters[kind]['input']:
        raise BusinessError('HEADER_ONLY', f'{kind} 证据只有表头')
    by_id = {}
    for row in records:
        by_id.setdefault(row['id'].strip(), []).append(row)
    selected = []
    for key, rows in by_id.items():
        identities = {tuple((k, v) for k, v in r.items() if not k.startswith('_')) for r in rows}
        if len(identities) > 1:
            for row in rows:
                quarantine.append({'kind': kind, 'line': row['_line'], 'id': key,
                                   'code': 'EVIDENCE_DUP_CONFLICT',
                                   'detail': f'{kind} 同一编号存在不同原值',
                                   'raw': {k: v for k, v in row.items() if not k.startswith('_')}})
        else:
            rows[0]['id'] = key
            selected.append(rows[0])
            counters[kind]['duplicate'] += len(rows) - 1
    return selected, encoding


def validate_inspection(row, quarantine):
    out = dict(row)
    try:
        version = number(row['report_version'])
        if version < 1 or version % 1:
            raise BusinessError('INVALID_RANGE', '报告版本须为正整数')
        target = row['replaces'].strip()
        if target:
            replaced = number(target)
            if replaced < 1 or replaced % 1:
                raise BusinessError('INVALID_RANGE', '替代版本须为正整数')
            out['replaces'] = str(int(replaced))
        else:
            out['replaces'] = ''
        instant(row['sampled_at'])
        if row['verdict'].strip() not in VERDICTS:
            raise BusinessError('INVALID_RANGE', '抽检结论仅支持 不合格/合格')
        if not row['batch_no'].strip() or not row['voyage_no'].strip():
            raise BusinessError('UNKNOWN_REFERENCE', '来源批或船次缺失')
        out['report_version'] = int(version)
        out['verdict'] = row['verdict'].strip()
        out['batch_no'] = row['batch_no'].strip()
        out['voyage_no'] = row['voyage_no'].strip()
        return out
    except BusinessError as error:
        quarantine.append({'kind': 'inspections', 'line': row['_line'], 'id': row['id'],
                           'code': error.code, 'detail': error.message,
                           'raw': {k: v for k, v in row.items() if not k.startswith('_')}})
        return None


def validate_sale(row, quarantine):
    out = dict(row)
    try:
        sold = number(row['sold_kg'])
        if sold < 0:
            raise BusinessError('INVALID_RANGE', '成交货量不能为负')
        instant(row['sold_at'])
        if not row['ticket'].strip():
            raise BusinessError('UNKNOWN_REFERENCE', '票号缺失')
        out['sold_kg'] = str(sold)
        out['ticket'] = row['ticket'].strip()
        out['batch_no'] = row['batch_no'].strip()
        out['buyer_id'] = row['buyer_id'].strip()
        out['buyer_name'] = row['buyer_name'].strip()
        return out
    except BusinessError as error:
        quarantine.append({'kind': 'sales', 'line': row['_line'], 'id': row['id'],
                           'code': error.code, 'detail': error.message,
                           'raw': {k: v for k, v in row.items() if not k.startswith('_')}})
        return None


def validate_destination(row, quarantine):
    out = dict(row)
    try:
        recalled = number(row['recalled_kg'])
        if recalled < 0:
            raise BusinessError('INVALID_RANGE', '已召回货量不能为负')
        instant(row['traced_at'])
        if row['trace_status'].strip() not in TRACE_STATUS:
            raise BusinessError('INVALID_RANGE', '去向状态仅支持 已核清/失联')
        if not row['ticket'].strip():
            raise BusinessError('UNKNOWN_REFERENCE', '票号缺失')
        out['recalled_kg'] = str(recalled)
        out['ticket'] = row['ticket'].strip()
        out['trace_status'] = row['trace_status'].strip()
        out['buyer_id'] = row['buyer_id'].strip()
        return out
    except BusinessError as error:
        quarantine.append({'kind': 'destinations', 'line': row['_line'], 'id': row['id'],
                           'code': error.code, 'detail': error.message,
                           'raw': {k: v for k, v in row.items() if not k.startswith('_')}})
        return None
