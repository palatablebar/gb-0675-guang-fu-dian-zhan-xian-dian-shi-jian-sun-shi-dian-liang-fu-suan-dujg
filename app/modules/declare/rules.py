from decimal import Decimal
import hashlib, json
from app.coerce import number, instant, money
from app.errors import BusinessError

RECEIPTS = {'已回执', '待回执'}
Q4 = Decimal('.0001')

def _fail(message):
    raise BusinessError('BASIS_INVALID', message)

def _window(raw, index):
    if not isinstance(raw, dict):
        _fail(f'电价窗口{index}须为对象')
    try:
        start, end = instant(raw['start']), instant(raw['end'])
        price = number(raw['price_per_kwh'])
    except KeyError:
        _fail(f'电价窗口{index}缺少起止时间或电价')
    if not start < end:
        _fail(f'电价窗口{index}起止时间无效')
    if price >= 0:
        _fail(f'电价窗口{index}电价须为负值')
    return {'id': str(raw.get('id') or f'PW-{index}'), 'start': start.isoformat(),
            'end': end.isoformat(), 'price_per_kwh': str(price)}

def _order(raw, index, event_ids, strict_events):
    if not isinstance(raw, dict):
        _fail(f'指令{index}须为对象')
    try:
        event_id = str(raw['event_id']).strip()
        cap = number(raw['cap_kw'])
        receipt = str(raw['receipt']).strip()
    except KeyError:
        _fail(f'指令{index}缺少限电事件、上限或回执字段')
    if strict_events and event_ids is not None and event_id not in event_ids:
        _fail(f'指令{index}引用的限电事件{event_id}不存在')
    if cap < 0:
        _fail(f'指令{index}上限不能为负')
    if receipt not in RECEIPTS:
        _fail(f'指令{index}回执状态须为已回执或待回执')
    return {'id': str(raw.get('id') or f'ORD-{index}'), 'event_id': event_id,
            'cap_kw': str(cap), 'receipt': receipt}

def normalize_basis(raw, event_ids=None, strict_events=False):
    """校验并规范化申报依据；任何字段非法整体失败，由调用方保持旧版。"""
    if not isinstance(raw, dict):
        _fail('申报依据须为对象')
    if 'declare_deadline' not in raw:
        _fail('申报依据缺少申报截止时间')
    deadline = instant(raw['declare_deadline'])
    as_of = instant(raw['declare_as_of']) if raw.get('declare_as_of') else deadline
    windows = sorted((_window(w, i) for i, w in enumerate(raw.get('price_windows') or [], 1)),
                     key=lambda w: (w['start'], w['id']))
    for earlier, later in zip(windows, windows[1:]):
        if later['start'] < earlier['end']:
            _fail(f"电价窗口{earlier['id']}与{later['id']}重叠")
    orders = sorted((_order(o, i, event_ids, strict_events) for i, o in enumerate(raw.get('orders') or [], 1)),
                    key=lambda o: o['id'])
    return {'price_windows': windows, 'orders': orders,
            'declare_deadline': deadline.isoformat(), 'declare_as_of': as_of.isoformat()}

def basis_from_context(context):
    raw = {k: context[k] for k in ('price_windows', 'orders', 'declare_deadline', 'declare_as_of') if k in context}
    return raw or None

def basis_digest(basis):
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def _overlap_seconds(start, end, windows):
    total = 0
    for window in windows:
        edge = min(end, instant(window['end'])) - max(start, instant(window['start']))
        total += max(0, int(edge.total_seconds()))
    return min(total, int((end - start).total_seconds()))

def classify_segment(row, basis, events):
    """把单个损失片段拆成申报损失、主动降功率、待查三桶，保留判定依据。"""
    start, end = instant(row['start']), instant(row['end'])
    windows = [w for w in basis['price_windows'] if _overlap_seconds(start, end, [w])]
    covering = [o for o in basis['orders']
                if o['event_id'] in events
                and events[o['event_id']].get('device') == row['device']
                and instant(events[o['event_id']]['start']) <= start and end <= instant(events[o['event_id']]['end'])]
    cap = min((number(o['cap_kw']) for o in covering), default=None)
    base = {**row, 'order_ids': sorted(o['id'] for o in covering),
            'cap_kw': str(cap) if cap is not None else None,
            'price_per_kwh': windows[0]['price_per_kwh'] if windows else None}
    if row['loss_kwh'] is None:
        return {**base, 'declared_kwh': None, 'voluntary_kwh': None, 'pending_kwh': None,
                'bucket': '待查', 'causes': ['LOSS_UNKNOWN']}
    hours = Decimal(row['seconds']) / 3600
    loss, actual = number(row['loss_kwh']), number(row['actual_kw'])
    baseline = number(row['baseline_kw'])
    provable = min(loss, max(baseline - cap, Decimal(0)) * hours) if cap is not None else Decimal(0)
    remainder = loss - provable
    overlap = _overlap_seconds(start, end, windows)
    if not remainder or overlap >= row['seconds']:
        voluntary, unattributed = remainder, Decimal(0)
    elif overlap == 0:
        voluntary, unattributed = Decimal(0), remainder
    else:  # 部分落入负电价窗口，按重叠时长比例拆分
        voluntary = remainder * overlap / row['seconds']
        unattributed = remainder - voluntary
    receipt_pending = any(o['receipt'] != '已回执' for o in covering)
    declared = provable if covering and not receipt_pending else Decimal(0)
    pending = (provable if covering and receipt_pending else Decimal(0)) + unattributed
    causes = []
    if not covering: causes.append('NO_ORDER')
    if declared: causes.append('ORDER_CONFIRMED')
    if covering and receipt_pending and provable: causes.append('RECEIPT_PENDING')
    if voluntary: causes.append('VOLUNTARY_PRICE')
    if unattributed: causes.append('UNATTRIBUTED')
    if not loss: causes.append('NO_LOSS')
    parts = {'申报': declared, '主动': voluntary, '待查': pending}
    nonzero = [name for name, value in parts.items() if value > 0]
    bucket = nonzero[0] if len(nonzero) == 1 else ('拆分' if nonzero else '无损失')
    return {**base, 'declared_kwh': str(declared), 'voluntary_kwh': str(voluntary),
            'pending_kwh': str(pending), 'bucket': bucket, 'causes': causes}

def _sum(rows, key):
    return sum((number(r[key]) for r in rows if r[key] is not None), Decimal(0))

def build_declaration(energy_rows, basis, events):
    rows = [classify_segment(r, basis, events) for r in energy_rows]
    known = [r for r in rows if r['loss_kwh'] is not None]
    unknown = [r for r in rows if r['loss_kwh'] is None]
    total, declared = _sum(rows, 'loss_kwh'), _sum(rows, 'declared_kwh')
    voluntary, pending = _sum(rows, 'voluntary_kwh'), _sum(rows, 'pending_kwh')
    used = {oid for r in rows for oid in r['order_ids']}
    receipts_pending = sorted(o['id'] for o in basis['orders'] if o['id'] in used and o['receipt'] != '已回执')
    amounts = [number(r['declared_kwh']) * number(r['price_per_kwh']) for r in known
               if number(r['declared_kwh']) and r['price_per_kwh'] is not None]
    quant = lambda v: v.quantize(Q4)
    summary = {'known_loss_kwh': str(quant(total)), 'declared_kwh': str(quant(declared)),
               'voluntary_kwh': str(quant(voluntary)), 'pending_kwh': str(quant(pending)),
               'split_balanced': quant(declared + voluntary + pending) == quant(total),
               'unknown_segments': len(unknown), 'unknown_seconds': sum(r['seconds'] for r in unknown),
               'pending_segments': sum(1 for r in rows if r['pending_kwh'] is None or number(r['pending_kwh']) > 0),
               'receipts_pending': receipts_pending,
               'orders_inactive': sorted(o['id'] for o in basis['orders'] if o['id'] not in used),
               'reference_amount': money(sum(amounts, Decimal(0))) if amounts else None}
    if summary['pending_segments'] or receipts_pending:
        ruling = {'decision': '暂定申报',
                  'reason': '回执未齐或存在待查片段：按已确认部分先行申报，未确认片段保持待查，回执齐后经依据更新转正式'}
    else:
        ruling = {'decision': '正式申报', 'reason': '指令回执齐全，损失全部确认，形成正式申报'}
    ruling.update({'deadline': basis['declare_deadline'], 'as_of': basis['declare_as_of'],
                   'before_deadline': basis['declare_as_of'] < basis['declare_deadline']})
    return {'rows': rows, 'summary': summary, 'ruling': ruling,
            'basis': basis, 'basis_digest': basis_digest(basis)}

def apply(state):
    events = {r['id']: r for r in state['records'] if r['kind'] == 'EVENT'}
    raw = basis_from_context(state['context'])
    if raw is None:
        state['declaration'] = None
        state['views']['declare'] = {'title': '申报裁定', 'rows': [], 'metrics': {'裁定': '无申报依据'}}
        return
    basis = normalize_basis(raw, set(events))
    declaration = build_declaration(state['energy_rows'], basis, events)
    state['declaration'] = declaration
    s = declaration['summary']
    state['views']['declare'] = {'title': '申报裁定', 'rows': declaration['rows'],
                                 'metrics': {'申报损失_kWh': s['declared_kwh'], '主动降功率_kWh': s['voluntary_kwh'],
                                             '待查_kWh': s['pending_kwh'], '待查片段': s['pending_segments'],
                                             '裁定': declaration['ruling']['decision']}}
