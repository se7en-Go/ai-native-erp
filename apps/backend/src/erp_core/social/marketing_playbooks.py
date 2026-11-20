"""Bilingual operator playbooks for the fourteen social platforms.

Each card is an operating recipe, not marketing copy: who the audience is, the exact
path from a post to a lead, the practices that survive platform review, and an honest
statement of what the official API allows.

Whether a platform exposes an official publishing API is never duplicated here: the
service derives it from the locked `publish_mode` in `erp_core.social.playbooks`, so a
`manual_export` platform always reports `official_api_supported=False` and the operator
must publish from the official console.
"""

from __future__ import annotations

from typing import NamedTuple

from erp_core.social.playbooks import SocialPlatform, SocialRegion


class MarketingPlaybook(NamedTuple):
    platform: SocialPlatform
    region: SocialRegion
    name_zh: str
    name_en: str
    category_zh: str
    category_en: str
    audience_zh: str
    audience_en: str
    lead_funnel_zh: str
    lead_funnel_en: str
    best_practices_zh: tuple[str, ...]
    best_practices_en: tuple[str, ...]
    compliance_note_zh: str
    compliance_note_en: str


MARKETING_PLAYBOOKS: tuple[MarketingPlaybook, ...] = (
    MarketingPlaybook(
        platform="instagram",
        region="overseas",
        name_zh="Instagram",
        name_en="Instagram",
        category_zh="视觉种草 / 内容社区",
        category_en="Visual discovery and lifestyle community",
        audience_zh="欧美年轻消费者与家居、服饰、POD 独立站买家，决策链路短、看重视觉质感。",
        audience_en=(
            "Young consumers in North America and Europe, plus home, apparel and "
            "print-on-demand store owners who decide fast and judge by visuals."
        ),
        lead_funnel_zh="Reels 与轮播图种草 → 主页引导私信或落地页 → 私信收集 RFQ → 转入 CRM 线索。",
        lead_funnel_en=(
            "Reels and carousels create demand → profile CTA to DM or landing page → "
            "DM captures the RFQ → the thread is converted into a CRM lead."
        ),
        best_practices_zh=(
            "用模型光效场景图做首图，参数放第二张，避免首图堆字",
            "Hashtag 只保留 3–5 个精准采购词，不做堆砌",
            "评论咨询 1 小时内回复，越窗主动消息必须人工加标签",
        ),
        best_practices_en=(
            "Lead with a styled product shot, keep specifications on the second slide",
            "Use 3-5 precise sourcing hashtags instead of hashtag stuffing",
            "Answer comments within the hour; messenger re-opened threads need a human tag",
        ),
        compliance_note_zh=(
            "通过 Meta Graph API 与 Instagram 专业号发布；越窗私信仅允许人工使用 HUMAN_AGENT "
            "标签且不超过 7 天，AI 不得自动触发。"
        ),
        compliance_note_en=(
            "Publishing runs through the Meta Graph API for a professional account. "
            "Messaging outside the 24-hour window requires a human HUMAN_AGENT tag within "
            "7 days; the AI may never trigger it."
        ),
    ),
    MarketingPlaybook(
        platform="tiktok",
        region="overseas",
        name_zh="TikTok",
        name_en="TikTok",
        category_zh="短视频内容电商",
        category_en="Short-video commerce",
        audience_zh="欧美与东南亚内容电商人群，喜欢工厂实拍与工艺细节，冲动型下单比例高。",
        audience_en=(
            "Content-commerce buyers in North America, Europe and Southeast Asia who "
            "reward factory-floor footage and buy impulsively."
        ),
        lead_funnel_zh="工厂实拍短视频 → 评论区提问或挂车 → 私信/小店承接 → 询盘进 CRM。",
        lead_funnel_en=(
            "Factory-floor short video → comment questions or product link → DM or shop "
            "checkout → inquiry lands in CRM."
        ),
        best_practices_zh=(
            "前 3 秒直接展示成品效果，不铺垫",
            "竖屏 9:16 加原生字幕，覆盖静音观看场景",
            "Content Posting API 未过审前只能发布仅自己可见内容，先完成审核",
        ),
        best_practices_en=(
            "Show the finished result in the first three seconds",
            "Shoot native 9:16 with burned-in captions for muted viewing",
            "Until the Content Posting API audit passes, posts stay private-only",
        ),
        compliance_note_zh=(
            "使用 Content Posting API；审核未通过前只能上传仅自己可见内容，"
            "严禁跨平台搬运他人素材。"
        ),
        compliance_note_en=(
            "Uses the Content Posting API. Until the audit passes, uploads are visible only "
            "to the account owner, and re-uploading other creators' footage is prohibited."
        ),
    ),
    MarketingPlaybook(
        platform="facebook",
        region="overseas",
        name_zh="Facebook",
        name_en="Facebook",
        category_zh="社群与本地分销",
        category_en="Community and regional distribution",
        audience_zh="欧美社区、区域分销商与本地中小企业主，看重信任与本地服务能力。",
        audience_en=(
            "Local communities, regional distributors and small business owners in North "
            "America and Europe who buy on trust and local service."
        ),
        lead_funnel_zh="主页图文与社群分享 → 评论 Private Reply → Messenger 询盘 → CRM 线索。",
        lead_funnel_en=(
            "Page posts shared into groups → comment private reply → Messenger inquiry → "
            "CRM lead."
        ),
        best_practices_zh=(
            "主页与 IG 用同一套商务授权，避免重复维护",
            "贴文结尾写清下一步动作（询价、领样、看目录）",
            "进群前先读群规，先给价值再谈产品",
        ),
        best_practices_en=(
            "Authorize the page and IG together instead of maintaining two grants",
            "End every post with one explicit next step (quote, sample, catalogue)",
            "Read the group rules first and contribute before promoting",
        ),
        compliance_note_zh=(
            "通过 Graph API 发布主页内容；评论转私信需按 Private Replies 规则调用，"
            "禁止抓取群组成员批量私信。"
        ),
        compliance_note_en=(
            "Page publishing runs through the Graph API. Comment-to-DM must follow the "
            "Private Replies rules, and scraping group members for bulk DMs is forbidden."
        ),
    ),
    MarketingPlaybook(
        platform="youtube",
        region="overseas",
        name_zh="YouTube",
        name_en="YouTube",
        category_zh="长视频搜索资产",
        category_en="Long-form search asset",
        audience_zh="欧美高客单决策者与工程采购，倾向用长视频完成选型与供应商比价。",
        audience_en=(
            "High-ticket decision makers and engineering buyers in North America and Europe "
            "who shortlist suppliers through long-form video."
        ),
        lead_funnel_zh="选型长视频 → 简介与置顶评论放 B2B 询盘入口 → 邮件询盘 → CRM 机会。",
        lead_funnel_en=(
            "Selection video → description and pinned comment carry the B2B inquiry link → "
            "email inquiry → CRM opportunity."
        ),
        best_practices_zh=(
            "标题写清规格、场景与认证关键词，便于采购搜索",
            "加章节时间轴，方便采购跳读参数与安装",
            "上传配额有限，先规划月度选题再拍",
        ),
        best_practices_en=(
            "Put specification, application and certification keywords in the title",
            "Add chapter markers so buyers can jump to specs and installation",
            "Upload quota is limited, so plan the monthly slate before shooting",
        ),
        compliance_note_zh=(
            "通过 YouTube Data API 上传；上传配额与项目审核状态相关，未过审项目曾被限制为私享。"
        ),
        compliance_note_en=(
            "Uploads use the YouTube Data API. Quota depends on project audit status, and "
            "unaudited projects have been limited to private uploads."
        ),
    ),
    MarketingPlaybook(
        platform="linkedin",
        region="overseas",
        name_zh="LinkedIn",
        name_en="LinkedIn",
        category_zh="B2B 决策链触达",
        category_en="B2B decision-chain reach",
        audience_zh="欧美 B2B 采购、经销商与工程公司决策链，关注交付能力与合规资质。",
        audience_en=(
            "Procurement leads, distributors and engineering firms in North America and "
            "Europe who evaluate delivery capability and compliance."
        ),
        lead_funnel_zh=(
            "公司主页案例长文 → 行业话题评论互动 → 员工个人号转发 → 站内私信转邮件询盘。"
        ),
        lead_funnel_en=(
            "Company-page case study → comment on industry threads → employees reshare → "
            "in-mail conversation moves to email RFQ."
        ),
        best_practices_zh=(
            "案例写清认证、产线与交付周期，少用营销形容词",
            "每周固定 2 次行业话题互动，先评论再私信",
            "公开内容与内部资料分开，报价不进贴文",
        ),
        best_practices_en=(
            "Case studies state certifications, lines and lead times instead of adjectives",
            "Comment on industry threads twice a week before sending any in-mail",
            "Keep pricing out of posts; share it only through a private quote",
        ),
        compliance_note_zh=(
            "公司主页发布需 Community Management API 的 Vetted Review；个人动态读取权限已关闭，"
            "仅可管理自有主页与授权内容。"
        ),
        compliance_note_en=(
            "Company-page publishing needs a Vetted Review for the Community Management API. "
            "Reading personal feeds is closed; only owned pages and consented content apply."
        ),
    ),
    MarketingPlaybook(
        platform="pinterest",
        region="overseas",
        name_zh="Pinterest",
        name_en="Pinterest",
        category_zh="搜索型灵感图库",
        category_en="Search-driven inspiration board",
        audience_zh="欧美家居、照明、服饰与 POD 卖家，带着明确采购或搭配意图搜索。",
        audience_en=(
            "Home, lighting, apparel and print-on-demand sellers in North America and "
            "Europe who search with a concrete sourcing or styling intent."
        ),
        lead_funnel_zh=(
            "场景 Pin 引流独立站 → 选型指南图集沉淀搜索 → 落地页留邮箱 → 邮件培育转询盘。"
        ),
        lead_funnel_en=(
            "Scenario Pin drives traffic to the store → selection guide boards rank in search "
            "→ landing page captures email → nurture turns it into an inquiry."
        ),
        best_practices_zh=(
            "2:3 竖图 + 关键词写进 Pin 标题与描述",
            "落地页主题必须与 Pin 一致，避免跳出",
            "季节性主题提前 45 天建 Board",
        ),
        best_practices_en=(
            "Use 2:3 vertical images and put the keyword in the Pin title and description",
            "Keep the landing page on the same topic as the Pin",
            "Build seasonal boards roughly 45 days ahead",
        ),
        compliance_note_zh=(
            "需 Standard Access 才可公开发布；未获 Standard Access 的账号只能受限发布。"
        ),
        compliance_note_en=(
            "Public publishing requires Standard Access; accounts without it are limited to "
            "restricted publishing."
        ),
    ),
    MarketingPlaybook(
        platform="x",
        region="overseas",
        name_zh="X",
        name_en="X",
        category_zh="行业观点与快讯",
        category_en="Industry commentary and news",
        audience_zh="欧美科技、贸易与供应链从业者，关注行业事件与新品快讯。",
        audience_en=(
            "Technology, trade and supply-chain professionals in North America and Europe "
            "who follow industry events and product news."
        ),
        lead_funnel_zh="新品快讯串文 → 行业话题互动 → 主页链接到目录 → 询盘进 CRM。",
        lead_funnel_en=(
            "New-product thread → industry conversation → profile link to the catalogue → "
            "inquiry lands in CRM."
        ),
        best_practices_zh=(
            "首条给结论，后续条目再展开参数",
            "单条控制在 280 字符内，图片说明写进正文",
            "带链接贴文成本更高，按租户预算控制频率",
        ),
        best_practices_en=(
            "Put the conclusion in the first post, then expand the specs",
            "Stay within 280 characters and describe images in the body",
            "Linked posts cost more, so cap their cadence per tenant budget",
        ),
        compliance_note_zh="API 按量计费，带 URL 贴文单价更高，需按租户用量做预算与限频控制。",
        compliance_note_en=(
            "The API is metered and posts containing URLs cost more, so usage budgets and "
            "rate limits are enforced per tenant."
        ),
    ),
    MarketingPlaybook(
        platform="xiaohongshu",
        region="domestic",
        name_zh="小红书",
        name_en="Xiaohongshu (RED)",
        category_zh="种草笔记 / 真实体验",
        category_en="Recommendation notes and lived experience",
        audience_zh="一二线城市年轻女性与家居美学人群，重视真实体验、颜值与避坑细节。",
        audience_en=(
            "Young urban consumers in tier-1 and tier-2 cities who value authentic "
            "experience, aesthetics and honest pitfalls."
        ),
        lead_funnel_zh=(
            "达人与合集笔记种草 → 评论区承接咨询 → 专业号私信 → 微信或表单留资 → CRM 线索。"
        ),
        lead_funnel_en=(
            "Creator and roundup notes → comments routed to the professional account → DM → "
            "contact capture → CRM lead."
        ),
        best_practices_zh=(
            "3:4 封面 + 20 字内标题，首图放真实使用场景",
            "正文写场景与避坑细节，少喊口号",
            "话题标签 5–8 个，覆盖品类、人群与场景",
        ),
        best_practices_en=(
            "Use a 3:4 cover with a sub-20-character title showing real usage",
            "Write about scenarios and pitfalls instead of slogans",
            "Attach 5-8 tags covering category, audience and scenario",
        ),
        compliance_note_zh=(
            "通用营销 SaaS 无 write_notes 权限：导出成片与文案后需在官方创作中心人工确认发布，"
            "严禁私信群控外挂。"
        ),
        compliance_note_en=(
            "No general marketing SaaS holds write_notes authority: export the finished assets "
            "and text, then publish manually in the official creation console. DM automation "
            "plug-ins are prohibited."
        ),
    ),
    MarketingPlaybook(
        platform="douyin",
        region="domestic",
        name_zh="抖音",
        name_en="Douyin",
        category_zh="短视频与直播成交",
        category_en="Short video and live commerce",
        audience_zh="国内下沉市场与年轻消费人群，直播讲解与短视频成交效率高。",
        audience_en=(
            "Lower-tier and younger domestic consumers who convert quickly through live "
            "explanation and short video."
        ),
        lead_funnel_zh=(
            "工厂实拍短视频 → 直播间讲解与团购 → 评论区咨询承接 → 私信留资 → 企业微信跟进。"
        ),
        lead_funnel_en=(
            "Factory short video → live explanation and group buy → comment follow-up → DM "
            "contact capture → WeCom follow-up."
        ),
        best_practices_zh=(
            "前 3 秒展示成品效果，工艺细节留到中段",
            "竖屏原生字幕，适配静音刷屏场景",
            "挂车商品与内容主题必须一致，避免违规",
        ),
        best_practices_en=(
            "Show the finished result in the first three seconds, detail the process later",
            "Burn in native vertical captions for muted scrolling",
            "Keep linked products on the same topic as the video",
        ),
        compliance_note_zh=(
            "应用私信权限已回收且 h5.share 禁止商品营销类应用：仅提供成片打包与人工发布指引。"
        ),
        compliance_note_en=(
            "DM scopes have been withdrawn and h5.share blocks commerce-marketing apps, so "
            "only asset packaging plus manual publishing guidance is offered."
        ),
    ),
    MarketingPlaybook(
        platform="kuaishou",
        region="domestic",
        name_zh="快手",
        name_en="Kuaishou",
        category_zh="信任型产业带电商",
        category_en="Trust-based industry-belt commerce",
        audience_zh="国内信任型电商人群与产业带买家，老铁关系带来的复购稳定。",
        audience_en=(
            "Trust-driven domestic buyers and industry-belt merchants whose repeat orders "
            "follow established relationships."
        ),
        lead_funnel_zh="产业带溯源短视频 → 直播带货 → 粉丝群复购 → 私信/电话询盘进 CRM。",
        lead_funnel_en=(
            "Industry-belt traceability video → live selling → fan-group repeat purchase → "
            "DM or phone inquiry into CRM."
        ),
        best_practices_zh=(
            "真人出镜讲工艺与质检，建立信任",
            "直播前 3 天发预热短视频蓄水",
            "售后响应时效写进直播话术",
        ),
        best_practices_en=(
            "Put a real person on camera explaining process and inspection",
            "Warm the audience with short videos three days before a live session",
            "State the after-sales response time in the live script",
        ),
        compliance_note_zh="无通用第三方发布接口，走成片导出与官方后台人工发布。",
        compliance_note_en=(
            "There is no general third-party publishing interface; export the assets and "
            "publish manually in the official console."
        ),
    ),
    MarketingPlaybook(
        platform="bilibili",
        region="domestic",
        name_zh="B站",
        name_en="Bilibili",
        category_zh="技术向中长视频",
        category_en="Technical mid/long-form video",
        audience_zh="国内年轻技术爱好者与测评受众，愿意看完整参数与拆解。",
        audience_en=(
            "Young domestic technology enthusiasts and reviewers who watch full teardowns "
            "and parameter walk-throughs."
        ),
        lead_funnel_zh=(
            "UP 主测评合作 → 技术长视频沉淀搜索 → 评论区与私信咨询 → 表单留资 → CRM 线索。"
        ),
        lead_funnel_en=(
            "Creator review collaboration → technical long-form ranks in search → comment "
            "and DM questions → form capture → CRM lead."
        ),
        best_practices_zh=(
            "封面信息层级清晰，一眼看出品类与卖点",
            "分 P 讲安装、参数与实测，方便采购跳读",
            "评论区置顶选型指引与询价入口",
        ),
        best_practices_en=(
            "Keep the cover hierarchy obvious: category first, differentiator second",
            "Split parts into installation, specs and testing for easy skimming",
            "Pin the selection guide and inquiry entry in the comments",
        ),
        compliance_note_zh="无通用自动发布接口，需人工上传并遵守社区规范与恰饭标注规则。",
        compliance_note_en=(
            "No general automated publishing interface exists; uploads are manual and must "
            "follow community rules including sponsored-content disclosure."
        ),
    ),
    MarketingPlaybook(
        platform="wechat_video",
        region="domestic",
        name_zh="微信视频号",
        name_en="WeChat Channels",
        category_zh="私域关系链传播",
        category_en="Private-domain relationship reach",
        audience_zh="国内微信生态私域客户与经销商，熟人关系链带来高信任传播。",
        audience_en=(
            "Existing private-domain customers and distributors in the WeChat ecosystem "
            "where acquaintance chains carry high trust."
        ),
        lead_funnel_zh="视频号内容 → 直播间预约 → 企业微信承接 → 一对一报价 → 订单。",
        lead_funnel_en=(
            "Channels video → live reservation → WeCom hand-off → one-to-one quote → order."
        ),
        best_practices_zh=(
            "竖屏 9:16，口播先给结论再讲工艺",
            "直播间挂载企业微信入口，缩短留资路径",
            "与公众号图文互导，形成内容闭环",
        ),
        best_practices_en=(
            "Use 9:16 and state the conclusion before explaining the process",
            "Attach the WeCom entry inside the live room to shorten capture",
            "Cross-link with the official-account articles to close the loop",
        ),
        compliance_note_zh="无第三方发布 API，需人工在视频号助手发布；绝不使用个人微信外挂。",
        compliance_note_en=(
            "There is no third-party publishing API; publishing is manual in the Channels "
            "assistant and personal-WeChat plug-ins are never used."
        ),
    ),
    MarketingPlaybook(
        platform="wechat_oa",
        region="domestic",
        name_zh="微信公众号",
        name_en="WeChat Official Account",
        category_zh="私域深度内容与客服",
        category_en="Private-domain long-form and service",
        audience_zh="国内私域订阅用户与 B2B 经销商，适合深度内容与客服承接。",
        audience_en=(
            "Domestic private-domain subscribers and B2B distributors who consume deep "
            "content and expect service follow-up."
        ),
        lead_funnel_zh="深度图文 → 菜单承接选型入口 → 48 小时客服消息 → 表单留资 → CRM 机会。",
        lead_funnel_en=(
            "In-depth article → menu entry for selection help → 48-hour service message → "
            "form capture → CRM opportunity."
        ),
        best_practices_zh=(
            "草稿与发布分离，终稿必须人工确认",
            "标题包含采购搜索关键词",
            "客服消息严格在 48 小时窗口内发送",
        ),
        best_practices_en=(
            "Separate drafts from publishing and confirm the final copy by hand",
            "Include sourcing keywords in the title",
            "Send service messages strictly inside the 48-hour window",
        ),
        compliance_note_zh=(
            "仅通过官方第三方平台草稿/发布接口操作，客服消息受限 48 小时窗口与条数限制。"
        ),
        compliance_note_en=(
            "Only the official third-party draft/publish interfaces are used, and service "
            "messages stay inside the 48-hour window and message-count limits."
        ),
    ),
    MarketingPlaybook(
        platform="weibo",
        region="domestic",
        name_zh="微博",
        name_en="Weibo",
        category_zh="公共话题与品牌声量",
        category_en="Public topics and brand reach",
        audience_zh="国内公共话题参与者与品牌关注人群，适合活动曝光与新品发布。",
        audience_en=(
            "Domestic public-topic participants and brand followers, suited to campaign "
            "exposure and product launches."
        ),
        lead_funnel_zh="话题活动 → 超话与粉丝群运营 → KOL 转发 → 私信或落地页留资 → CRM 线索。",
        lead_funnel_en=(
            "Hashtag campaign → super-topic and fan-group operation → KOL reshare → DM or "
            "landing-page capture → CRM lead."
        ),
        best_practices_zh=(
            "话题词统一，活动全程不换词",
            "配图带品牌与产品信息，方便二传",
            "舆情 2 小时内响应，避免沉默发酵",
        ),
        best_practices_en=(
            "Lock one hashtag for the whole campaign",
            "Put brand and product facts in the image for easy resharing",
            "Respond to sentiment within two hours to prevent escalation",
        ),
        compliance_note_zh="发布走官方后台人工确认，舆情监听仅采集公开内容。",
        compliance_note_en=(
            "Publishing is confirmed manually in the official console, and sentiment "
            "listening collects public content only."
        ),
    ),
)

MARKETING_PLAYBOOK_BY_PLATFORM: dict[SocialPlatform, MarketingPlaybook] = {
    item.platform: item for item in MARKETING_PLAYBOOKS
}


def marketing_playbooks(region: str = "all") -> list[MarketingPlaybook]:
    """Return the locked playbook cards, optionally narrowed to one region."""
    if region == "all":
        return list(MARKETING_PLAYBOOKS)
    return [item for item in MARKETING_PLAYBOOKS if item.region == region]

