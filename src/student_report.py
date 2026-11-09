"""Readable weekly digest for a first-year marine surveying student."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import requests
try:
    from . import editorial as ed
    from .periods import content_version
except ImportError:
    import editorial as ed
    from periods import content_version


def render(g, db, report_dir, since_days, limit, translate, config=None):
    conn=g.init_db(db)
    sources={s['id']:s for s in g.load_sources(config)} if config else None
    g.recalculate_scores(conn,sources)
    report_dir.mkdir(parents=True,exist_ok=True)
    date=g.ACTIVE_PERIOD['id'] if g.ACTIVE_PERIOD else g.datetime.now(g.REPORT_TIMEZONE).date().isoformat()
    rows=g.report_candidates(conn,report_dir,since_days,min(limit,15),date)
    last=conn.execute('SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1').fetchone()
    statuses=json.loads(last['sources_json']) if last else []
    if sources is not None:
        observed={s['id'] for s in statuses}
        for sid,source in sources.items():
            if sid not in observed:
                statuses.append(dict(id=sid,name=source['name'],status='失败',optional=source.get('optional',False),recent=0,detail='本轮没有该来源的采集记录',metrics={}))
    core=[s for s in statuses if s['status']!='正常' and not s.get('optional')]
    optional=[s for s in statuses if s['status']!='正常' and s.get('optional')]
    zero=[s for s in statuses if not s['recent']]
    columns={r[1] for r in conn.execute('PRAGMA table_info(articles)')}
    if 'translation_input_hash' not in columns:
        conn.execute("ALTER TABLE articles ADD COLUMN translation_input_hash TEXT DEFAULT ''")
    policy=[r for r in rows if r['policy_relevance']>0]
    research=[r for r in rows if r not in policy and r['content_type'] in ('科研成果线索','数据与代码线索')]
    marine=[r for r in rows if r not in policy+research and r['marine_relevance']>0]
    other=[r for r in rows if r not in policy+research+marine]
    now=datetime.now(g.REPORT_TIMEZONE).isoformat(timespec='seconds')
    lines=[f'# 海洋测绘学习与科研周报（{date}）','',
           '> 读者：山东科技大学青岛校区测绘工程（海洋测绘特色班）大一学生。重点：认识方向、积累基础、发现可验证的小项目。','']
    if g.ACTIVE_PERIOD:
        lines += [f"- 新闻周期（北京时间）：{g.ACTIVE_PERIOD['start']} ≤ 发布日期 < {g.ACTIVE_PERIOD['end']}。",'- 本期日期表示所属周，延迟补发或本周重跑不会移动新闻窗口。']
    else:lines += [f'- 调试窗口：最近 {since_days} 天。']
    lines += [f'- 生成时间：{now}。',f'- 精选 {len(rows)} 条；海洋相关 {sum(r["marine_relevance"]>0 for r in rows)} 条；一般可迁移测绘方法最多 2 条；实际发布媒体每家最多 3 条。',
              '- 摘要采用正文关键句节选与自动翻译；学习任务为编辑建议。厂商声明、新闻报道、正式论文与公开数据分别看待。',
              '- 不为凑篇数降低门槛；标题线索、融资、人物、科普活动及会议消息不进入技术主栏。','']
    if core:lines += ['> **核心来源采集覆盖不完整：** '+'、'.join(s['name'] for s in core)+'。本期先发布已有结果，缺失来源不算“没有新闻”。','']
    if optional:lines += ['> **补充来源降级：** '+'、'.join(s['name'] for s in optional)+'。不阻断核心报告，但覆盖有所减少。','']
    if zero:lines += [f'> {len(zero)} 个来源本轮未筛到近期候选，不能据此断言该网站本周没有相关新闻。','']
    lines += ['## 本周怎么读','',
              '1. 先看海洋测绘与数据线索，选 1 条读原文；留意它是研究、数据发布，还是设备应用。',
              '2. 再看文末的学习任务卡，每周最多做一个小任务，先保证数学、英语、Python 和测量基础课程。',
              '3. 找到论文/数据入口后再与老师讨论研究题目；新闻关键词分数不等于科研水平或保研加分。','']
    session=requests.Session();session.headers.update({'User-Agent':g.USER_AGENT})
    cache={};cards=[]
    try:
        overrides=json.loads((g.ROOT/'config'/'editorial_overrides.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):overrides={}
    translation_version=content_version(g.ROOT)

    def tr(text):
        out=g.translate_text(text,session,cache) if translate else text
        if 'hydrographic' in text.lower():out=out.replace('水文测量','海道测量').replace('水道测量','海道测量')
        if 'hydrographic survey' in text.lower():out=out.replace('水文调查','海道测量')
        if 'hydrography' in text.lower():out=out.replace('水文学','海道测量')
        if 'underwater survey' in text.lower():out=out.replace('水下调查','水下测量')
        if 'lawnmower' in text.lower():out=out.replace('割草机模式调查','往复式平行测线测量').replace('割草机模式测量','往复式平行测线测量')
        if 'mäerl' in text.lower():out=out.replace('mäerl 矿床','mäerl 藻床').replace('Mäerl 矿床','Mäerl 藻床')
        if 'seabed habitats' in text.lower():out=out.replace('海底栖息地','海底生境')
        out=out.replace('本周的本周地图','本周地图')
        if 'Coast Survey' in text:out=out.replace('海岸调查','海岸测量')
        if 'UK Hydrographic Office' in text:out=out.replace('英国海道测量办公室','英国海道测量局')
        if 'uncrewed surface vessel' in text.lower():out=out.replace('无人水面舰艇','无人水面艇')
        return out

    def append(title,selected):
        lines.extend([f'## {title}',''])
        if not selected:
            lines.extend(['本期未筛到已核实日期、满足内容门槛的新条目；这不代表领域内没有更新。',''])
            return
        for i,r in enumerate(selected,1):
            excerpt=ed.evidence_sentences(r['title'],r['summary'],r['content'])
            input_hash=hashlib.sha256((translation_version+r['title']+excerpt).encode()).hexdigest()
            old_hash=conn.execute('SELECT translation_input_hash FROM articles WHERE id=?',(r['id'],)).fetchone()[0]
            if old_hash==input_hash and r['title_zh'] and r['summary_zh'] and (not translate or g.has_chinese(r['summary_zh'])):
                zh,summary_zh=r['title_zh'],r['summary_zh']
            else:
                zh,summary_zh=tr(r['title']),tr(excerpt)
                conn.execute('UPDATE articles SET title_zh=?,summary_zh=?,translation_input_hash=? WHERE id=?',(zh,summary_zh,input_hash,r['id']))
            url=r['canonical_url'] or r['url']
            note=overrides.get(url,{})
            zh=note.get('title_zh') or zh
            summary_zh=note.get('summary_zh') or summary_zh
            summary_label='人工核对摘要' if note.get('summary_zh') else '原文节选'
            body_status=ed.body_level(r['title'],r['summary'],r['content'])
            lines.extend([f'### {i}. {zh}','',f'- 原标题：{r["title"]}',f'- 来源：{r["source_name"]}',
                          f'- 日期（北京时间）：{datetime.fromisoformat(r["published_at"]).astimezone(g.REPORT_TIMEZONE).isoformat()}；依据：{r["date_basis"] or "来源发布日期"}。',
                          f'- 内容类型：{r["content_type"]}；正文状态：{body_status}。',
                          f'- 原文：[{url}]({url})','',f'**中文要点（{summary_label}）：** {summary_zh}',''])
            if note.get('review_note'):
                lines.extend([f'**核查说明：** {note["review_note"]}',''])
            if not g.has_chinese(r['title']):
                lines.extend(['<details><summary>展开英文关键句，对照专业术语</summary>','',excerpt,'','</details>','',
                              f'**翻译状态：** {"人工核对中文摘要；上方英文保留原报道措辞" if note.get("summary_zh") else ("自动翻译，术语已做规则修正，仍需对照原文" if g.has_chinese(summary_zh) else "翻译服务未成功，保留原文")}。',''])
            if r['content_type']=='技术与工程进展':
                lines.extend(['**阅读定位：** 装备发布、合同或工程应用可用于认识测量链；仅凭新闻不能确认独立精度或算法优越性。',''])
            evidence=g.extract_research_evidence((r['content'] or '')+' '+(r['summary'] or ''))
            if r in research or any(evidence.values()):
                lines.append('**论文、数据与验证依据（仅列原文实际出现的入口）：**')
                for label,values in evidence.items():
                    missing={'DOI':'未找到论文 DOI，未确认对应论文。','数据或代码链接':'未找到公开入口，未确认数据或代码开放。','精度指标原句':'未找到误差指标，不据此判断精度。'}
                    lines.append(f'- {label}：'+('；'.join(values) if values else missing.get(label,'已采集文本中未找到。')))
                lines.append('')
            task=ed.student_task(r['title'],r['content'],r['content_type'])
            if task:
                lines.extend([f'**与你的关系：** 可归入“{task[0]}”方向。{task[1]}',''])
                # At most two different tasks; do not overwhelm a first-year student.
                if len(cards)<2 and task[0] not in [c[1][0] for c in cards]:cards.append((zh,task))

    append('海洋科研与公开数据线索',research)
    append('海洋测绘技术与工程应用',marine)
    append('十五五与国际海洋规划、标准更新',policy)
    if not policy:
        lines += ['正式文件阅读入口与已核实/待核实范围见 [规划与标准观察清单](../docs/planning-baseline.md)。旧文件不冒充本周新闻。','']
    append('可迁移的测绘方法（最多 2 条）',other)
    lines += ['## 大一阶段：本周只做一个小任务','']
    for i,(name,task) in enumerate(cards,1):
        lines += [f'### 任务 {i}：{task[0]}',f'- 对应新闻：{name}',f'- 先修基础：{task[1]}',f'- 操作：{task[2]}',f'- 验收：{task[3]}','']
    if not cards:lines += ['本期暂未形成足够可信的专项任务；继续完成已有 QGIS 地形可视化与 Python 栅格基础，不为新闻数量临时换题。','']
    lines += ['## 筛选透明度','']
    lower=(datetime.fromisoformat(g.ACTIVE_PERIOD['start']).astimezone(timezone.utc).isoformat() if g.ACTIVE_PERIOD else (datetime.now(timezone.utc)-g.timedelta(days=since_days)).isoformat())
    upper=(datetime.fromisoformat(g.ACTIVE_PERIOD['end']).astimezone(timezone.utc).isoformat() if g.ACTIVE_PERIOD else datetime.now(timezone.utc).isoformat())
    rejects=conn.execute("SELECT eligibility_reason,COUNT(*) n FROM articles WHERE published_at>=? AND published_at<? AND eligibility_reason!='' GROUP BY eligibility_reason",(lower,upper)).fetchall()
    lines += [f'- 当前周期数据库中保留的未入选资格原因：'+('；'.join(f'{r[0]} {r[1]} 条' for r in rejects) or '无')+'。',
              '- 原始记录保留；更新配置会重新判定资格，不通过手工删除新闻掩盖筛选问题。',
              '- 同事件优先官方/正文充分的稿件；重复转载不占第二个名额。相同产品后续的新事件允许再次入选。','']
    campus=conn.execute("SELECT * FROM articles WHERE source_id LIKE 'sdust_%' AND content_type='科研资助与机会（非成果）' AND published_at>=? AND published_at<? ORDER BY published_at DESC LIMIT 2",(lower,upper)).fetchall()
    if campus:
        lines += ['## 本校方向观察（不计技术新闻）','',
                  '以下是本校平台的项目公示，可用于认识学院研究方向；公示不是大一学生报名通知，也不是新技术成果。','']
        for r in campus:
            url=r['canonical_url'] or r['url']
            lines += [f'- [{r["title"]}]({url})（{datetime.fromisoformat(r["published_at"]).astimezone(g.REPORT_TIMEZONE).date().isoformat()}）。先了解平台与课题名称，感兴趣再向老师询问本科生参与途径。']
        lines.append('')
    pending=conn.execute("SELECT * FROM articles WHERE source_id LIKE 'google_news_%' AND eligibility_reason LIKE '%标题%' AND published_at>=? AND published_at<? ORDER BY marine_relevance DESC",(lower,upper)).fetchall()
    pending=[r for r in pending if not any(ed.same_event(r,old) for old in rows)][:3]
    if pending:
        lines += ['<details><summary>待核实的聚合标题（不计入精选，不据此判断技术成果）</summary>','',
                  '以下只有聚合标题，尚未确认发布者正文及原站日期；可能是旧文重新出现。','']
        for r in pending:lines.append(f'- [{r["title"]}]({r["url"]})：原文与日期待核实。')
        lines += ['','</details>','']
    lines += ['## 新闻源采集状态','',f'采集时间：{last["checked_at"] if last else "无本轮采集记录"}','',
              '| 来源 | 状态 | 列表/订阅原始链接 | 日期窗口内 | 正文可用 | 精选 | 说明 |',
              '| --- | --- | ---: | ---: | ---: | ---: | --- |']
    for s in statuses:
        m=s.get('metrics',{})
        s['selected']=sum(r['source_id']==s['id'] for r in rows)
        detail=s.get('detail','').replace('|','/')
        detail += f"；主题过滤 {m.get('topic_rejected',0)}，窗外 {m.get('outside_window',0)}，日期缺失 {m.get('date_missing',0)}"
        if m.get('detail_budget_skipped'):detail+=f"；详情预算未访问 {m['detail_budget_skipped']}"
        lines.append(f"| {s['name']} | {s['status']}{'（补充）' if s.get('optional') else ''} | {m.get('raw_links','未记录')} | {m.get('in_window',s['recent'])} | {m.get('body_available','未记录')} | {s['selected']} | {detail} |")
    lines += ['','“正常”表示本轮列表/订阅读取成功；不保证所有正文可用或全站覆盖。详情访问、主题排除等完整计数保存在 health.json。','']
    output=report_dir/f'weekly-{date}.md'
    output.write_text('\n'.join(lines),encoding='utf-8')
    # Same-period rerun replaces membership instead of accumulating removed items.
    conn.execute('DELETE FROM report_items WHERE report_date=?',(date,))
    for r in rows:conn.execute('INSERT OR IGNORE INTO report_items VALUES (?,?,?)',(date,g.normalize_url(r['canonical_url'] or r['url']),r['title']))
    old_health={}
    try:old_health=json.loads((report_dir/'health.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    version=content_version(g.ROOT)
    attempt=(old_health.get('attempts',0) if old_health.get('period_id')==date and old_health.get('content_version')==version else 0)+1
    health={'report_date':date,'period_id':date,'period':g.ACTIVE_PERIOD,'checked_at':last['checked_at'] if last else None,
            'generated_at':now,'complete':bool(statuses) and not core,'core_complete':bool(statuses) and not core,
            'supplement_degraded':bool(optional),'selected':len(rows),'sources':statuses,
            'config_hash':last['config_hash'] if last else '', 'content_version':version,
            'rule_version':ed.VERSION,'attempts':attempt,
            'quality':{'marine':sum(r['marine_relevance']>0 for r in rows),'research_or_data':len(research),
                       'policy':len(policy),'title_only_in_main':sum(ed.body_level(r['title'],r['summary'],r['content'])=='仅有标题，待核实' for r in rows),'zero_candidate_sources':len(zero)},
            'manual_review':'规则筛选和节选；没有全网召回率保证，科研价值需结合导师意见。'}
    (report_dir/'health.json').write_text(json.dumps(health,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    conn.commit();conn.close();session.close()
    return output
