"""Evidence-aware editorial decisions; no model keys or fabricated research claims."""
import re
from urllib.parse import urlsplit
from rapidfuzz.fuzz import ratio

VERSION = 'marine-student-v2'
LOW_PRIORITY = ('商业市场资讯', '商业融资与合作', '人物与科普活动', '会议与校园活动', '行业问卷与人事消息', '科研资助与机会（非成果）')
OCEAN = ('海洋', '海底', '水下', '海岸', '近岸', '海道', '海图', '海山', '深海',
         'ocean', 'marine', 'seabed', 'seafloor', 'underwater', 'hydrograph', 'bathymetr',
         'water depth', 'nautical', 'coastal', 'offshore')
METHOD = ('测深', '测绘', '测量', '地形', '声呐', '遥感', '导航', '点云', '数据', '海图',
          'survey', 'mapping', 'sonar', 'bathymetr', 'depth', 'data', 'lidar', 'chart',
          'photogrammetr', 'navigation', 'altimetry', '水色', '光学', '反演')
RESOURCE = re.compile(r'https?://(?:doi\.org/|github\.com/|zenodo\.org/|figshare\.com/|[^/\s]*pangaea\.de/|emodnet\.ec\.europa\.eu/geoviewer|emodnet\.ec\.europa\.eu/en/seabed-habitats|data\.marine\.copernicus\.eu/|www\.gebco\.net/data)[^\s<>"\']*', re.I)


def hit(text, terms):
    text = (text or '').lower()
    return any(term.lower() in text for term in terms)


def genre(title, lead):
    if hit(title,('industry survey','take part in','appoints','appointed as','任命','就任')):return '行业问卷与人事消息'
    if hit(title,('基金拟资助','开放基金','项目申报','项目公示')):return '科研资助与机会（非成果）'
    if hit(title, ('market worth', 'market size', 'market forecast', 'marketsandmarkets', '市场规模', '市场预测', '市场报告')):
        return '商业市场资讯'
    if hit(title, ('series a', 'series b', 'funding round', 'investors back', 'nine-figure deal', 'acquisition', '融资', '收购', '天使轮', '上市', 'signs agreement', '签署合作协议', '签约仪式')):
        return '商业融资与合作'
    if hit(title, ('spotlight: meet', 'meet our', 'obituary', 'monopoly', '运动会', '讣告', '人物专访', '开放日', 'pre-orders', 'staff profile')):
        return '人物与科普活动'
    if hit(title, ('highlights and outcomes from the workshop', '成功举办', '召开会议', '参观', '党建', '主题党日', '运动会', '获佳绩', '挑战杯', '篮球', '羽毛球', '排球', '团体赛', '升旗', '换届', '座谈', '讲座', '大讲堂', '论坛举行', '学术报告会')):
        return '会议与校园活动'
    # Attribution alone does not prove a new research result. Require a research lead.
    if hit(lead, ('发表论文', '研究进展', '研究成果', '研究方面取得', '发表于', 'published in',
                  'research paper', 'peer-reviewed', 'journal of', 'doi.org/', '研究构建')):
        return '科研成果线索'
    if hit(title,('图志','数据产品','数据集发布','map of the week')) or hit(lead, ('开源', '数据发布', 'open-source', 'open dataset', 'data release', 'publicly available dataset')):
        return '数据与代码线索'
    if hit(title, ('规划', '纲要', '行动计划', '实施方案', '战略', '标准', 's-100', 's-102', 'strategic plan', 'roadmap', 'standard')):
        return '政策与标准报道'
    if hit(lead, METHOD):
        return '技术与工程进展'
    return '行业资讯'


def marine_context(title, lead):
    # Headline and article lead must establish the subject, not a passing mention deep in the page.
    text = title + ' ' + lead[:1300]
    if hit(title,('seabed','seafloor','海底')) and hit(title,('map','地图','空间信息')):return True
    if hit(text,('水深','测深','海图','声呐','bathymetr','echosounder','water depth')):return True
    return hit(text, OCEAN) and hit(text, METHOD)


def body_level(title, summary, content):
    text = (content or '').strip()
    if title and text.startswith(title) and len(text)-len(title)<120:return '仅有标题，待核实'
    if len(text) >= 180 and len(text) > len(title) + 80:
        return '正文可用'
    if len(summary or '') >= 100:
        return '仅有站点摘要'
    return '仅有标题，待核实'


def publisher_domain(url, fallback=''):
    return (urlsplit(url).hostname or fallback).lower().removeprefix('www.')


def event_key(title, content=''):
    text = title + ' ' + content[:1400]
    # Names identify the subject; action/date checks below identify the event.
    if '海山图志' in text or 'Seamount Atlas' in text:
        return 'seamount-atlas'
    names = re.findall(r'[《“"]([^》”"]{3,60})[》”"]', title)
    return '|'.join(sorted(names))


def same_event(a, b):
    def get(x,k):
        try:return x[k] or ''
        except (KeyError,IndexError):return ''
    ua = get(a,'canonical_url') or get(a,'url')
    ub = get(b,'canonical_url') or get(b,'url')
    if ua and ua == ub:
        return True
    # A later publication about the same subject is eligible as a new development.
    da,db = get(a,'published_at')[:10],get(b,'published_at')[:10]
    try:
        from datetime import date
        close = abs((date.fromisoformat(da)-date.fromisoformat(db)).days) <= 3
    except ValueError:
        close = False
    if not close:return False
    ta,tb=get(a,'title'),get(b,'title')
    if ratio(ta.lower(),tb.lower()) >= 88:return True
    ca,cb=get(a,'content'),get(b,'content')
    ka,kb=event_key(ta,ca),event_key(tb,cb)
    if ka and ka==kb:
        actions=('发布','launch','released','正式','上线','成果')
        if hit(ta+' '+ca[:300],actions) and hit(tb+' '+cb[:300],actions):return True
    if len(ca)>180 and len(cb)>180 and ratio(ca[:1600],cb[:1600])>=90 and ratio(ta,tb)>=60:return True
    # Chinese rewritten headlines for the same uniquely described release.
    if all('海山' in t and '成果' in t and '发布' in t for t in (ta,tb)):
        return True
    return False


def evidence_sentences(title, summary, content):
    text=(content or summary or '').strip()
    text=re.sub(r'\s+',' ',text)
    # Remove repeated headline; split sentences without splitting decimal precision.
    if text.startswith(title):text=text[len(title):].strip()
    parts=[s.strip() for s in re.split(r'(?<=[。！？])|(?<=[.!?])\s+(?=[A-Z“])',text) if len(s.strip())>35]
    parts=[s for s in parts if not hit(s,('continue reading','subscribe to','cookie','privacy policy','sign up for our'))]
    if not parts:return (summary or text)[:1000]
    if 'map of the week' in title.lower():
        focused=[s for s in parts if hit(s,('this week’s map','this week\'s map','this week’s','map layer titled','modelled spatial','download data','catalogue'))]
        if focused:return ' '.join(focused[:3])
    def priority(s):
        terms=('方法','构建','多波束','数据','精度','反演','分辨率','公开','model','method','accuracy','dataset','database','resolution','rmse','uncertainty','algorithm','laser scanning','download','map layer','drift')
        value=sum(term in s.lower() for term in terms)
        value+=2*bool(re.search(r'\d+(?:\.\d+)?\s*(?:%|m\b|cm\b|million|billion|米|厘米)',s))
        value-=3*hit(s,('our ambition','targeting the','founded in','subscribe','aim to engage','we gained valuable','according to'))
        return value
    picks=[0]+sorted(range(1,len(parts)),key=lambda i:(priority(parts[i]),-i),reverse=True)[:2]
    selected=[]
    for i in sorted(set(picks)):
        if sum(len(s) for s in selected)+len(parts[i])<=1900:selected.append(parts[i])
    return ' '.join(selected) or parts[0]


def student_task(title, content, category):
    text=title+' '+content[:3000]
    if category in LOW_PRIORITY:
        return None
    if hit(title,('图志','map of the week')):
        return ('海底地形与公开数据可视化','先学 QGIS 栅格、图层叠加、经纬度/投影和分辨率；图志发布不代表原始数据开放。',
                '检查原文的数据下载入口和许可。若不能取得图志数据，用已有 GEBCO 小区域格网练习地形图，并明确它是练习数据。',
                '交付 1 张有比例尺、色带、单位、数据来源和分辨率的地图，加 200 字误差/适用尺度说明；不把 GEBCO 粗网格当浅水精度真值。')
    if hit(title,('auv','usv','rov','无人船','无人艇','无人潜器')):
        return ('无人海洋测量系统','先认识平台、定位导航、测深载荷和数据处理的分工，暂不购买硬件。',
                '画出该系统的组成图，分别列出新闻公开的参数、厂商声称的性能和缺失的独立验证；思考定位误差如何传到水深点。',
                '交付 1 张系统图和 3 个可以问老师的问题；不要把价格、续航或导航漂移直接等同于水深测量精度。')
    if hit(text,('水深','测深','bathymetr','water depth','uav photos')) and hit(text,('遥感','光学','卫星','无人机','satellite','optical','photos','photogrammetr')):
        return ('浅水水深反演', '先学 Python 栅格读取、波段比值和独立验证；本周只核对论文与数据入口，暂不复现整套算法。',
                '做一张资料表：研究区、影像日期、分辨率、实测水深来源、潮位/垂直基准、训练与验证划分、RMSE。',
                '交付 1 张资料表；只有确认独立参考水深后再做反演。GEBCO 粗网格不能充当米级浅水精度真值。')
    if hit(text,('多波束','multibeam','point cloud','点云','声呐','sonar')):
        return ('多波束与点云质量控制', '先学坐标、深度正负号、点云散点图和稳健统计；设备采购消息只用于认识测量链。',
                '画出传感器—定位姿态—声速—潮位/垂直基准—滤波—精度评价流程，区分论文验证结果与厂商标称参数。',
                '交付 1 张测量流程图和误差来源表；找到可下载数据后再写离群点检测代码。')
    if hit(text,('图志','海底','海山','seabed','seafloor','gebco','emodnet','bathymetr','数据集','dataset')):
        return ('海底地形与公开数据可视化', '先学 QGIS 栅格、经纬度/投影、分辨率和垂直基准；适合大一从读图入手。',
                '核对本条数据是否公开和许可；可用时截取一个小区域，制作水深色带图并标明数据年份、单位、分辨率。',
                '交付 1 张规范地图和 200 字局限说明；新闻发布不等于数据已经开放下载。')
    if hit(text,('s-100','s-102','标准','规划','roadmap','strategy')):
        return ('标准与方向阅读', '当前以理解术语和数据结构为主，不要求大一直接实现生产级系统。',
                '保存正式发布页，记录发布机构、版本、适用时间及与水深/海图数据的关系；将新闻与正式标准分开。',
                '交付 1 页术语与版本表；只把原文明确出现的内容写成规划结论。')
    if hit(text,('无人','auv','usv','rov','autonomous')):
        return ('无人海洋测量系统', '先理解船/潜器、定位导航、测深载荷和数据处理分工。',
                '比较本条系统的测量任务、载荷、定位方法与试验条件，找出没有公开的精度和数据。',
                '交付 1 张系统组成图；当前不需要购买硬件或开展海上实测。')
    return ('测绘方法基础', '优先完成数学、英语、程序设计和测量基础课，科研阅读每周挑 1 条深入即可。',
            '找出原文中的任务、方法、数据与验证依据；记录可向老师询问的一个具体问题。',
            '交付 200 字阅读笔记；尚无论文或数据时，只作为方向认识。')


GLOSSARY = {
 'hydrographic survey':'海道测量', 'hydrography':'海道测量',
 'bathymetric lidar':'水深激光雷达', 'multibeam echosounder':'多波束回声测深仪',
 'bathymetry':'水深测量', 'seafloor mapping':'海底测绘',
 'UK Hydrographic Office':'英国海道测量局', 'Coast Survey':'海岸测量',
}


def protect_terms(text):
    for term,zh in sorted(GLOSSARY.items(),key=lambda x:-len(x[0])):
        text=re.sub(re.escape(term),zh,text,flags=re.I)
    return text
