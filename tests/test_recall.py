"""召回处置 M8 测试：来源批/买家/生效版本核对、裁决纪律、发布与重跑一致性。"""
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, store
from app.engine import evaluate
from app.modules.recall.evidence import EVIDENCE_FILES
from app.modules.recall.rules import _effective_reports

ROOT = Path(__file__).resolve().parents[1]


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'recall.sqlite'))
    return TestClient(main.app)


def _sample_texts():
    return {k: (ROOT / 'sample_data' / f'recall_{k}.csv').read_text() for k in EVIDENCE_FILES}


def test_effective_report_uses_late_correction():
    # 迟到更正：B-201 生效版本为 v2，已售票按 v2 裁决，不按出票时点放行
    s = evaluate()
    effective = {r['batch_no']: r for r in s['recall']['effective_reports']}
    assert effective['B-201']['report_version'] == 2
    assert effective['B-201']['verdict'] == '不合格'
    ticket = next(t for t in s['recall']['tickets'] if t['ticket'] == '03001')
    assert ticket['effective_version'] == 2
    assert ticket['result'] == '召回'


def test_stale_report_branch_blocks_ticket_not_safe():
    # 陈旧更正分支：来源批生效版无法确定，已售票只能暂缓，绝不判安全
    texts = _sample_texts()
    texts['inspections'] += 'insp-B201-3,B-201,V-07,黄鱼,新增项,2026-11-20T09:00:00+08:00,3,1,不合格\n'
    s = evaluate({'recall_evidence': texts})
    conflict = next(c for c in s['recall']['report_conflicts'] if c['batch_no'] == 'B-201')
    assert conflict['code'] == 'REPORT_STALE_BRANCH'
    for t in s['recall']['tickets']:
        if 'B-201' in t['batch_no']:
            assert t['result'] == '暂缓召回'
            assert t['code'] == 'REPORT_BRANCH_BLOCKED'
    assert s['recall']['order']['blocked'] >= 1
    assert s['recall']['order']['status'] == '待发布'


def test_missing_base_report_conflict():
    rows = [
        {'id': 'a', 'batch_no': 'B-X', 'voyage_no': 'V-1', 'fish': '黄鱼', 'fail_item': '',
         'sampled_at': '2026-11-18T06:00:00+08:00', 'report_version': 2, 'replaces': '1',
         'verdict': '合格', '_line': 1},
    ]
    effective, conflicts, _ = _effective_reports(rows)
    assert not effective
    assert conflicts[0]['code'] == 'REPORT_MISSING_BASE'


def test_untraced_source_batch_never_safe():
    # 03008 指向无任何抽检证据的 B-999
    s = evaluate()
    t = next(t for t in s['recall']['tickets'] if t['ticket'] == '03008')
    assert t['result'] == '暂缓召回'
    assert t['code'] == 'SOURCE_UNTRACED'


def test_buyer_lost_destination_never_safe():
    # 03004 买家失联：整票暂缓，已核清与失联部分分别记账
    s = evaluate()
    t = next(t for t in s['recall']['tickets'] if t['ticket'] == '03004')
    assert t['result'] == '暂缓召回'
    assert t['code'] == 'BUYER_UNREACHABLE'
    assert t['remaining_kg'] == '25'


def test_partial_destination_recall_keeps_remaining():
    s = evaluate()
    t = next(t for t in s['recall']['tickets'] if t['ticket'] == '03002')
    assert t['result'] == '召回'
    assert t['sold_kg'] == '40'
    assert t['recalled_kg'] == '25'
    assert t['remaining_kg'] == '15'


def test_multi_buyer_ticket_aggregation():
    # 一票两买家：03003 成交 30+20=50，已召回 30+10=40
    s = evaluate()
    t = next(t for t in s['recall']['tickets'] if t['ticket'] == '03003')
    assert t['sold_kg'] == '50'
    assert t['recalled_kg'] == '40'
    assert t['remaining_kg'] == '10'
    assert set(t['buyer_id'].split(',')) == {'K-103', 'K-108'}


def test_voyage_control_holds_then_releases_neighbor_batch():
    s = evaluate()
    controls = {(c['voyage_no'], c['batch_no']): c for c in s['recall']['voyage_controls']}
    assert controls[('V-07', 'B-201')]['level'] == '控制-不合格批'
    # 相邻合格批 B-202：其票 03005 已凭生效合格版核清 → 解除船次扣住
    assert controls[('V-07', 'B-202')]['level'] == '解除-核清相邻批'
    # 未核清的相邻批仍扣住：构造一张 B-202 无去向票
    texts = _sample_texts()
    texts['sales'] += 'sale-shadow,03010,B-202,黄鱼,12,2026-11-18T16:00:00+08:00,K-111,无去向商户\n'
    s2 = evaluate({'recall_evidence': texts})
    c2 = {(c['voyage_no'], c['batch_no']): c for c in s2['recall']['voyage_controls']}
    assert c2[('V-07', 'B-202')]['level'] == '控制-相邻批暂缓'
    shadow = next(t for t in s2['recall']['tickets'] if t['ticket'] == '03010')
    # 票本身对应生效合格批，但因无去向登记，整批的船次扣住不得解除（不提前判安全）
    assert shadow['effective_version'] == 1
    assert any(v['batch_no'] == 'B-202' and v['level'] == '控制-相邻批暂缓'
               for v in s2['recall']['voyage_controls'])


def test_pure_pass_voyage_not_controlled():
    s = evaluate()
    controls = {(c['voyage_no'], c['batch_no']): c for c in s['recall']['voyage_controls']}
    assert controls[('V-09', 'B-205')]['level'] == '不控制'
    t = next(t for t in s['recall']['tickets'] if t['ticket'] == '03009')
    assert t['result'] == '免召回'


def test_deterministic_rerun_same_order():
    # 同输入重跑：批号、处置单号、逐票结果完全一致
    a = evaluate()
    b = evaluate()
    assert a['manifest']['run_id'] == b['manifest']['run_id']
    assert a['recall']['order'] == b['recall']['order']
    assert a['recall']['tickets'] == b['recall']['tickets']
    # 打乱证据行顺序，裁决仍一致
    texts = _sample_texts()
    header, *body = texts['sales'].splitlines()
    shuffled = header + '\n' + '\n'.join(reversed(body)) + '\n'
    texts['sales'] = shuffled
    c = evaluate({'recall_evidence': texts})
    # 行号随证据排列变化属正常；裁决语义（不含溯源行号）必须逐票一致
    strip_lines = lambda rows: [{k: v for k, v in r.items() if k != 'evidence_lines'} for r in rows]
    assert strip_lines(c['recall']['tickets']) == strip_lines(a['recall']['tickets'])


def test_rerun_endpoint_restores_snapshot(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    rerun = c.post(f'/api/runs/{rid}/rerun', json={})
    assert rerun.status_code == 200
    again = rerun.json()
    assert again['manifest']['run_id'] == rid
    assert again['recall']['order']['order_no'] == run['recall']['order']['order_no']
    # 快照内保留三类原始证据字节
    for key in EVIDENCE_FILES:
        assert again['input_snapshot']['recall_raw_b64'][key]


def test_publish_idempotent_and_stale_revision(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    url = f'/api/recall/{rid}/publish'
    ok = c.post(url, json={'revision': 1})
    assert ok.status_code == 200
    assert ok.json()['publication']['order_no'] == run['recall']['order']['order_no']
    # 同单据重发幂等，不新增版本
    again = c.post(url, json={'revision': 1})
    assert again.status_code == 200
    assert len(again.json()['publication']['versions']) == 1
    # 复核推进版本后，旧版本发布返回 409
    c.post(f'/api/runs/{rid}/review', json={'revision': 1})
    stale = c.post(url, json={'revision': 1})
    assert stale.status_code == 409
    assert stale.json()['code'] == 'STALE_VERSION'


def test_publish_blocked_when_evidence_conflict(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    texts = _sample_texts()
    # 同编号不同原值 → 证据隔离，处置单不可发布，原始行保留在隔离清单
    first = texts['sales'].splitlines()[1]
    conflicting = first.replace(',60,', ',61,')
    assert conflicting.split(',')[0] == first.split(',')[0]
    texts['sales'] = texts['sales'].rstrip('\n') + '\n' + conflicting + '\n'
    run = c.post('/api/runs', json={'recall_evidence': texts}).json()
    rid = run['manifest']['run_id']
    assert run['recall']['order']['status'] == '待发布'
    assert any(q['code'] == 'EVIDENCE_DUP_CONFLICT' for q in run['recall']['quarantine'])
    resp = c.post(f'/api/recall/{rid}/publish', json={'revision': 1})
    assert resp.status_code == 422
    assert resp.json()['code'] == 'RECALLED_ORDER_BLOCKED'
    # 未留下任何发布记录（中断不产生半成品）
    assert 'publication' not in c.get(f'/api/runs/{rid}').json()


def test_publish_interruption_keeps_previous_version(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'recall.sqlite')
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    # 模拟首次发布写入中断：连接在 INSERT 前抛错，with 事务回滚，不产生半成品
    from app import store as store_mod
    real_connect = store_mod.connect

    class Boom(Exception):
        pass

    class ConnProxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, params=()):
            if sql.lstrip().startswith('INSERT INTO recall_publications'):
                raise Boom('interrupted')
            return self._con.execute(sql, params)

        def __getattr__(self, item):
            return getattr(self._con, item)

        def __enter__(self):
            self._con.__enter__()
            return self

        def __exit__(self, *exc):
            return self._con.__exit__(*exc)

    monkeypatch.setattr(store_mod, 'connect', lambda path: ConnProxy(real_connect(path)))
    import pytest
    with pytest.raises(Boom):
        store_mod.publish_recall(db_path, rid, 1, run['recall'])
    monkeypatch.undo()
    # 中断后无任何发布记录，批次裁决结果完好
    before = store.get(db_path, rid)
    assert 'publication' not in before
    assert before['recall']['tickets'] == run['recall']['tickets']
    # 恢复后重新发布成功，旧版逻辑结果不变，且仅一份发布记录
    state = store.publish_recall(db_path, rid, 1, run['recall'])
    assert state['publication']['order_no'] == run['recall']['order']['order_no']
    assert len(state['publication']['versions']) == 1
    assert state['recall']['tickets'] == run['recall']['tickets']
    # 同一处置单重复发布幂等
    again = store.publish_recall(db_path, rid, 1, run['recall'])
    assert len(again['publication']['versions']) == 1


def test_raw_evidence_no_loss_in_export(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    resp = c.get(f'/api/export/{rid}')
    assert resp.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(z.namelist())
    assert {'recall_evidence/recall_inspections.csv',
            'recall_evidence/recall_sales.csv',
            'recall_evidence/recall_destinations.csv',
            'recall_decision.json'} <= names
    texts = _sample_texts()
    for key, filename in (('inspections', 'recall_inspections.csv'),
                          ('sales', 'recall_sales.csv'),
                          ('destinations', 'recall_destinations.csv')):
        assert z.read(f'recall_evidence/{filename}').decode() == texts[key]
    decision = json.loads(z.read('recall_decision.json'))
    assert decision['order']['order_no'] == run['recall']['order']['order_no']


def test_evidence_quarantine_keeps_raw_row(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    texts = _sample_texts()
    # 列数错误行进入隔离，raw 保留原值
    texts['destinations'] += 'dst-bad,03001,K-101,10\n'
    run = c.post('/api/runs', json={'recall_evidence': texts}).json()
    q = next(q for q in run['recall']['quarantine'] if q['code'] == 'EVIDENCE_BAD_COLUMNS')
    assert q['raw']['id'] == 'dst-bad'
    assert q['kind'] == 'destinations'


def test_recall_page_and_api_registered(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.post('/api/recall', json={}).status_code == 200
    run = c.post('/api/runs', json={}).json()
    page = c.get('/recall?run_id=' + run['manifest']['run_id'])
    assert page.status_code == 200
    body = page.text
    for token in ('来源批', '买家', '生效版本', '处置结果', run['recall']['order']['order_no']):
        assert token in body


def test_bad_verdict_quarantined_not_crash():
    texts = _sample_texts()
    texts['inspections'] = texts['inspections'].replace('合格', '待定', 1)
    s = evaluate({'recall_evidence': texts})
    assert any(q['code'] == 'INVALID_RANGE' and q['kind'] == 'inspections'
               for q in s['recall']['quarantine'])


def test_over_recall_quantity_suspended_not_negative():
    # 已召回超过成交量：量冲突，暂缓核量，禁止出现负待召回
    texts = _sample_texts()
    texts['destinations'] = texts['destinations'].replace(
        'dst-03002,03002,K-102,25,', 'dst-03002,03002,K-102,99,')
    s = evaluate({'recall_evidence': texts})
    t = next(t for t in s['recall']['tickets'] if t['ticket'] == '03002')
    assert t['result'] == '暂缓召回'
    assert t['code'] == 'RECALL_OVER_QUANTITY'
    assert Decimal(t['remaining_kg']) >= 0
