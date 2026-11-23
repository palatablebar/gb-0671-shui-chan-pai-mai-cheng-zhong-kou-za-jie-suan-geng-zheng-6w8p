import io, json, zipfile, base64
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        snap=result.get('input_snapshot',{})
        for name,key in (('recall_inspections.csv','inspections'),
                         ('recall_sales.csv','sales'),
                         ('recall_destinations.csv','destinations')):
            b64=snap.get('recall_raw_b64',{}).get(key)
            if b64:
                archive.writestr('recall_evidence/'+name,base64.b64decode(b64))
        if result.get('recall'):
            archive.writestr('recall_decision.json',json.dumps({
                'order':result['recall']['order'],
                'tickets':result['recall']['tickets'],
                'voyage_controls':result['recall']['voyage_controls'],
                'effective_reports':result['recall']['effective_reports'],
                'report_audit':result['recall']['report_audit'],
                'report_conflicts':result['recall']['report_conflicts'],
                'quarantine':result['recall']['quarantine'],
                'counters':result['recall']['counters'],
                'raw_digest':result['recall']['raw_digest'],
                'publication':result.get('publication'),
            },ensure_ascii=False,indent=2))
    return buffer.getvalue()
