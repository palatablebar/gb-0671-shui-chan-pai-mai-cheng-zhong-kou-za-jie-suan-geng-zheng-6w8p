"""召回处置裁决。

裁决纪律（不可让渡的安全底线）：
1. 追不到来源批或买家去向的票，一律暂缓/召回，绝不判安全；
2. 来源批抽检或更正版本发生变化，已售票仍进入召回范围（按生效版裁决，不按出票时点放行）；
3. 船次控制单先把同船相邻来源批一并扣住（赶下架时点），逐票核清后再按票缩小范围；
4. 同输入重跑结果完全一致，裁决不依赖遍历顺序。
"""
from collections import defaultdict
from decimal import Decimal
from app.modules.recall.evidence import (
    load_evidence, validate_inspection, validate_sale, validate_destination,
    INSPECTION_FIELDS, SALE_FIELDS, DESTINATION_FIELDS)


def apply(state):
    recall_quarantine = []
    counters = {}
    raws = state['recall_raw']
    inspections_raw, enc_i = load_evidence(raws['inspections'], INSPECTION_FIELDS,
                                           'inspections', recall_quarantine, counters)
    sales_raw, _ = load_evidence(raws['sales'], SALE_FIELDS, 'sales',
                                 recall_quarantine, counters)
    destinations_raw, _ = load_evidence(raws['destinations'], DESTINATION_FIELDS,
                                        'destinations', recall_quarantine, counters)
    inspections = [r for r in (validate_inspection(r, recall_quarantine) for r in inspections_raw) if r]
    sales = [r for r in (validate_sale(r, recall_quarantine) for r in sales_raw) if r]
    destinations = [r for r in (validate_destination(r, recall_quarantine) for r in destinations_raw) if r]

    effective, report_conflicts, report_audit = _effective_reports(inspections)
    known_batches = {r['batch_no'] for r in inspections}
    batch_to_voyage = {r['batch_no']: r['voyage_no'] for r in inspections}
    # 成交行补上船次，供船级控制使用
    for sale in sales:
        sale['voyage_no'] = batch_to_voyage.get(sale['batch_no'], '')
    destinations_by_ticket = _group_destinations(destinations)
    traced_tickets = {t for t, rows in destinations_by_ticket.items()
                      if rows and all(r['trace_status'] == '已核清' for r in rows)}
    ticket_rows = decide_tickets(sales, effective, report_conflicts,
                                 destinations_by_ticket, known_batches)
    voyage_controls, _, _ = decide_voyages(
        ticket_rows, effective, known_batches, batch_to_voyage, traced_tickets)
    order = build_order(ticket_rows, voyage_controls, recall_quarantine,
                        effective, report_conflicts, counters, state['recall_digest'])

    state['recall'] = {
        'inspections': inspections,
        'sales': sales,
        'destinations': destinations,
        'effective_reports': [effective[b] for b in sorted(effective)],
        'report_conflicts': report_conflicts,
        'report_audit': report_audit,
        'tickets': ticket_rows,
        'voyage_controls': voyage_controls,
        'order': order,
        'quarantine': recall_quarantine,
        'counters': counters,
        'encoding': enc_i,
        'raw_digest': state['recall_digest'],
    }
    state['views']['recall'] = {
        'title': '召回处置',
        'rows': ticket_rows,
        'metrics': {
            '召回票': order['recall_tickets'],
            '暂缓票': order['suspended_tickets'],
            '免召回票': order['released_tickets'],
            '待召回kg': order['remaining_kg'],
            '阻断项': order['blocked'],
            '单据状态': order['status'],
        },
    }



def _effective_reports(inspections):
    """按来源批选出唯一生效抽检报告，沿用更正链纪律：只能替代当前生效版。"""
    grouped = defaultdict(list)
    for row in inspections:
        grouped[row['batch_no']].append(row)
    effective = {}
    conflicts = []
    audit = []
    for batch_no, versions in sorted(grouped.items()):
        current = None
        invalid = False
        for row in sorted(versions, key=lambda r: r['report_version']):
            target = row['replaces']
            if current is None:
                if target:
                    conflicts.append({'batch_no': batch_no, 'code': 'REPORT_MISSING_BASE',
                                      'report_version': row['report_version']})
                    invalid = True
                    break
            elif target != str(current['report_version']):
                conflicts.append({'batch_no': batch_no, 'code': 'REPORT_STALE_BRANCH',
                                  'report_version': row['report_version'],
                                  'expected': current['report_version']})
                invalid = True
                break
            audit.append({'batch_no': batch_no, 'report_version': row['report_version'],
                          'replaces': target, 'verdict': row['verdict'],
                          'fail_item': row['fail_item'], 'sampled_at': row['sampled_at'],
                          'line': row['_line']})
            current = row
        if current and not invalid:
            effective[batch_no] = current
    return effective, conflicts, audit


def _group_destinations(destinations):
    """一票可卖给多个买家；按票聚合去向，买家级别缺口逐户保留。"""
    by_ticket = defaultdict(list)
    for row in destinations:
        by_ticket[row['ticket']].append(row)
    return by_ticket


def decide_tickets(sales, effective, report_conflicts, destinations_by_ticket, known_batches):
    """逐票裁决。返回处置行列表（顺序固定，按票号排序）。"""
    sales_by_ticket = defaultdict(list)
    for row in sales:
        sales_by_ticket[row['ticket']].append(row)
    rows = []
    for ticket in sorted(sales_by_ticket):
        deals = sales_by_ticket[ticket]
        sold_total = sum((Decimal(d['sold_kg']) for d in deals), Decimal(0))
        buyers = sorted({d['buyer_id'] for d in deals if d['buyer_id']})
        buyer_names = sorted({d['buyer_name'] for d in deals if d['buyer_name']})
        batches = sorted({d['batch_no'] for d in deals})
        # 1) 来源批核验
        missing_batches = [b for b in batches if not b or b not in known_batches]
        if missing_batches:
            rows.append(_ticket_row(ticket, batches, buyers, buyer_names, deals, sold_total,
                                    '暂缓召回', 'SOURCE_UNTRACED',
                                    f'来源批 {",".join(missing_batches) or "空白"} 无抽检证据，禁止判安全'))
            continue
        effective_reports = [effective[b] for b in batches if b in effective]
        if any(b in {c['batch_no'] for c in report_conflicts} for b in batches) or \
                len(effective_reports) != len(batches):
            rows.append(_ticket_row(ticket, batches, buyers, buyer_names, deals, sold_total,
                                    '暂缓召回', 'REPORT_BRANCH_BLOCKED',
                                    '抽检更正链存在断链或陈旧分支，生效版本无法确定'))
            continue
        current = effective_reports[0]
        latest_version = max(r['report_version'] for r in effective_reports)
        corrected = latest_version > 1
        verdicts = {r['verdict'] for r in effective_reports}
        # 2) 生效版合格 → 放行；任何来源批不合格 → 进入召回/暂缓
        if verdicts == {'合格'}:
            rows.append(_ticket_row(ticket, batches, buyers, buyer_names, deals, sold_total,
                                    '免召回', 'EFFECTIVE_PASS',
                                    '生效抽检版本全部合格', latest_version))
            continue
        # 不合格来源批：裁决买家去向
        dests = destinations_by_ticket.get(ticket, [])
        if not dests:
            rows.append(_ticket_row(ticket, batches, buyers, buyer_names, deals, sold_total,
                                    '暂缓召回', 'DESTINATION_MISSING',
                                    '成交票无任何去向证据，禁止判安全', latest_version,
                                    fail_item=current['fail_item']))
            continue
        # 失联或买家对不上：整票暂缓，已核清部分照常召回
        lost = [d for d in dests if d['trace_status'] == '失联']
        deal_buyers = {d['buyer_id'] for d in deals if d['buyer_id']}
        unknown_buyer = [d for d in dests if d['buyer_id'] and d['buyer_id'] not in deal_buyers]
        recalled_total = sum((Decimal(d['recalled_kg']) for d in dests), Decimal(0))
        remaining = sold_total - recalled_total
        if remaining < 0:
            rows.append(_ticket_row(ticket, batches, buyers, buyer_names, deals, sold_total,
                                    '暂缓召回', 'RECALL_OVER_QUANTITY',
                                    f'已召回 {recalled_total}kg 超过成交 {sold_total}kg，证据量冲突待核',
                                    latest_version, recalled_kg=str(recalled_total),
                                    remaining_kg='0', fail_item=current['fail_item']))
            continue
        base = _ticket_row(ticket, batches, buyers, buyer_names, deals, sold_total,
                           None, None, None, latest_version,
                           recalled_kg=str(recalled_total), remaining_kg=str(remaining),
                           fail_item=current['fail_item'])
        if lost or unknown_buyer:
            base['result'] = '暂缓召回'
            codes = []
            if lost:
                codes.append('BUYER_LOST:' + ','.join(sorted({d['buyer_id'] or '空' for d in lost})))
            if unknown_buyer:
                codes.append('BUYER_MISMATCH:' + ','.join(sorted({d['buyer_id'] for d in unknown_buyer})))
            base['code'] = 'BUYER_UNREACHABLE'
            base['reason'] = '；'.join(codes) + '。已核清部分继续召回，未核清部分暂缓'
        else:
            base['result'] = '召回'
            base['code'] = 'RECALL_EFFECTIVE_VERSION'
            note = '按生效抽检版本执行召回'
            if corrected:
                note += f'（迟到更正至 v{latest_version}，已售票不按出票时点放行）'
            if remaining <= 0:
                note += '；货量已全部召回'
            base['reason'] = note
        rows.append(base)
    return rows


def _ticket_row(ticket, batches, buyers, buyer_names, deals, sold_total, result, code, reason,
                effective_version=None, recalled_kg='0', remaining_kg=None, fail_item=''):
    return {
        'ticket': ticket,
        'batch_no': ','.join(batches),
        'voyage_no': ','.join(sorted({d.get('voyage_no', '') for d in deals if d.get('voyage_no')})),
        'buyer_id': ','.join(buyers),
        'buyer_name': ','.join(buyer_names),
        'sold_kg': str(sold_total),
        'recalled_kg': recalled_kg,
        'remaining_kg': str(sold_total) if remaining_kg is None else remaining_kg,
        'effective_version': effective_version or '',
        'fail_item': fail_item,
        'result': result,
        'code': code,
        'reason': reason or '',
        'evidence_lines': sorted({d['_line'] for d in deals}),
    }


def decide_voyages(ticket_rows, effective, known_batches, batch_to_voyage, traced_tickets):
    """船次控制：同船存在不合格生效批时，相邻来源批先扣住（暂缓），逐票核清后可解除。"""
    failed_batches = {b for b, r in effective.items() if r['verdict'] == '不合格'}
    voyage_to_batches = defaultdict(set)
    for b, v in batch_to_voyage.items():
        voyage_to_batches[v].add(b)
    failed_voyages = {batch_to_voyage[b] for b in failed_batches if b in batch_to_voyage}
    # 票级结论按来源批汇总，并登记每批的已售票
    batch_ticket_state = defaultdict(set)
    batch_tickets = defaultdict(list)
    for row in ticket_rows:
        for b in row['batch_no'].split(','):
            batch_ticket_state[b].add(row['result'])
            batch_tickets[b].append(row['ticket'])
    controls = []
    for voyage_no in sorted(voyage_to_batches):
        for b in sorted(voyage_to_batches[voyage_no]):
            if b not in known_batches:
                continue
            report = effective.get(b)
            states = batch_ticket_state.get(b, set())
            if b in failed_batches:
                level = '控制-不合格批'
                action = '全批召回，已售逐票召回/暂缓'
            elif voyage_no in failed_voyages:
                # 相邻批解除条件（全部满足）：
                # 1) 有已售票可核；2) 全部凭生效合格版免召回；3) 每张票去向均已登记核清。
                # 任一票未核清或无票可核，都继续扣住，绝不提前放行。
                tickets = batch_tickets.get(b, [])
                all_traced = bool(tickets) and all(t in traced_tickets for t in tickets)
                if states == {'免召回'} and all_traced:
                    level = '解除-核清相邻批'
                    action = '逐票核清且生效版合格，解除船次扣住'
                else:
                    level = '控制-相邻批暂缓'
                    action = '同船存在不合格批，先扣住待逐票核清'
            else:
                level = '不控制'
                action = '生效版合格且同船无不合格批'
            controls.append({
                'voyage_no': voyage_no,
                'batch_no': b,
                'report_version': report['report_version'] if report else '',
                'verdict': report['verdict'] if report else '证据缺失',
                'fail_item': report['fail_item'] if report else '',
                'level': level,
                'action': action,
            })
    return controls, sorted(failed_voyages), sorted(failed_batches)


def build_order(ticket_rows, voyage_controls, evidence_conflicts, effective,
                report_conflicts, counters, raw_digest):
    """形成可发布召回处置单。证据级冲突未清前不得发布。"""
    blocked = len(evidence_conflicts) + len(report_conflicts)
    recall = [r for r in ticket_rows if r['result'] == '召回']
    suspended = [r for r in ticket_rows if r['result'] == '暂缓召回']
    released = [r for r in ticket_rows if r['result'] == '免召回']
    recall_kg = sum((Decimal(r['recalled_kg']) for r in recall), Decimal(0))
    remaining_kg = sum((Decimal(r['remaining_kg']) for r in recall), Decimal(0))
    suspended_kg = sum((Decimal(r['remaining_kg']) for r in suspended), Decimal(0))
    order = {
        'order_no': 'RC-' + raw_digest[:12],
        'status': '待发布' if blocked else '可发布',
        'blocked': blocked,
        'recall_tickets': len(recall),
        'suspended_tickets': len(suspended),
        'released_tickets': len(released),
        'recalled_kg': str(recall_kg),
        'remaining_kg': str(remaining_kg),
        'suspended_kg': str(suspended_kg),
        'controlled_voyages': sorted({c['voyage_no'] for c in voyage_controls
                                      if c['level'].startswith('控制')}),
    }
    return order
