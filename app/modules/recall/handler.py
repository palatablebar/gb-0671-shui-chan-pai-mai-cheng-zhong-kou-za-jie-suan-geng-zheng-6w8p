from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store
from app.errors import BusinessError

router = APIRouter()


@router.get('/recall')
def page(request: Request):
    return render(request, 'recall')


@router.post('/api/recall')
def calculate(payload: dict = Body(default={})):
    return evaluate(payload, until=8)


@router.post('/api/recall/{run_id}/publish')
def publish(run_id: str, payload: dict = Body(default={})):
    """发布召回处置单。

    - 证据阻断（隔离/报告断链）未清前拒绝发布；
    - 已发布单据按 order_no 幂等，重复发布不产生新记录；
    - 发布中断不会删除旧版：published_at 与单据在同一事务内落库。
    """
    from app.main import DB_PATH
    revision = payload.get('revision')
    if not isinstance(revision, int) or revision < 1:
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    state = store.get(DB_PATH, run_id)
    recall = state.get('recall')
    if not recall:
        raise BusinessError('INCOMPLETE_RUN', '该批次未执行召回裁决')
    if recall['order']['blocked']:
        raise BusinessError('RECALLED_ORDER_BLOCKED',
                            f"存在 {recall['order']['blocked']} 项证据阻断，处置单不可发布")
    return store.publish_recall(DB_PATH, run_id, revision, recall)
