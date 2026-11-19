import type { PlatformPlaybook, SocialPlatform } from "./types";

/**
 * Platform metadata only: publishing capability is declared per platform because most
 * domestic networks expose no third-party publishing API. Extend by appending entries.
 */
export const PLATFORM_PLAYBOOKS: readonly PlatformPlaybook[] = [
  {
    platform: "instagram", region: "overseas", label: "Instagram", emoji: "📸",
    audience: "欧美年轻消费者与生活方式买家，家居、服饰、POD 独立站转化高",
    acquisition_plays: ["Reels 种草 + 评论区转私信", "Story 投票收集意向", "达人合作与 UGC 二次分发"],
    best_practices: ["统一用 Unified Login 授权", "Carousel 首图放场景化成品", "Hashtag 控制 3–5 个精准词"],
    content_formats: ["Reels", "Carousel", "Story", "图文"],
    publish_mode: "api",
    compliance_note: "越窗主动消息仅允许人工 HUMAN_AGENT 标签且不超过 7 天，AI 不得自动触发。",
  },
  {
    platform: "tiktok", region: "overseas", label: "TikTok", emoji: "🎵",
    audience: "欧美与东南亚内容电商人群，冲动型消费与工厂实拍接受度高",
    acquisition_plays: ["工厂/生产线短视频", "短视频挂车与直播", "评论区问题转私信跟进"],
    best_practices: ["Content Posting API 需通过审核解除自见限制", "前 3 秒给成品效果", "竖屏 9:16 原生字幕"],
    content_formats: ["短视频", "图文轮播", "直播"],
    publish_mode: "api",
    compliance_note: "禁止跨平台纯搬运；审核未通过前只能发布仅自己可见内容。",
  },
  {
    platform: "youtube", region: "overseas", label: "YouTube", emoji: "▶️",
    audience: "欧美高客单决策者与工程采购，长内容影响 B2B 选型",
    acquisition_plays: ["安装/选型长视频", "Shorts 切片引流", "视频简介放 B2B 询盘入口"],
    best_practices: ["标题含规格与场景关键词", "章节时间轴便于采购跳读", "独立上传配额需要提前规划"],
    content_formats: ["长视频", "Shorts", "直播"],
    publish_mode: "api",
    compliance_note: "Data API 上传配额与项目审核状态相关，未过审项目曾被限制为私享。",
  },
  {
    platform: "facebook", region: "overseas", label: "Facebook", emoji: "👍",
    audience: "欧美社区与中小企业主，适合区域分销与本地服务获客",
    acquisition_plays: ["主页图文 + 社群分享", "评论自动私信承接", "Messenger 询盘进收件箱"],
    best_practices: ["主页与 IG 统一授权", "贴文附带明确行动指引", "群组互动守则先行"],
    content_formats: ["图文", "Reels", "直播"],
    publish_mode: "api",
    compliance_note: "评论转私信需按 Private Replies 规则调用，禁止抓取群组名单批量私信。",
  },
  {
    platform: "linkedin", region: "overseas", label: "LinkedIn", emoji: "💼",
    audience: "欧美 B2B 采购、经销商与工程公司决策链",
    acquisition_plays: ["公司主页案例长文", "行业话题评论互动", "员工个人号转发扩音"],
    best_practices: ["Community Management API 需 Vetted Review", "案例带认证与交付数据", "少用营销腔，多讲交付能力"],
    content_formats: ["长文", "图文", "文档轮播", "视频"],
    publish_mode: "api",
    compliance_note: "读取个人动态的权限已关闭，仅可管理自有主页与授权内容。",
  },
  {
    platform: "pinterest", region: "overseas", label: "Pinterest", emoji: "📌",
    audience: "欧美家居、照明、服饰与 POD 卖家搜索型流量",
    acquisition_plays: ["场景图 Pin 引流独立站", "选型指南图集", "季节性主题 Board 运营"],
    best_practices: ["需 Standard Access 才可公开发布", "2:3 竖图与关键词描述", "落地页与 Pin 主题一致"],
    content_formats: ["标准 Pin", "Idea Pin", "视频 Pin"],
    publish_mode: "api",
    compliance_note: "未获 Standard Access 的账号只能受限发布，需先完成应用审核。",
  },
  {
    platform: "x", region: "overseas", label: "X", emoji: "𝕏",
    audience: "欧美科技、贸易与供应链从业者，适合行业观点与快讯",
    acquisition_plays: ["新品快讯串文", "行业话题参与", "客户案例转发"],
    best_practices: ["单条 280 字符内", "带链接的贴文成本更高需评估", "串文首条给结论"],
    content_formats: ["短文本", "串文", "图片", "视频"],
    publish_mode: "api",
    compliance_note: "API 按量计费，带 URL 贴文单价更高，需按租户用量做预算控制。",
  },
  {
    platform: "xiaohongshu", region: "domestic", label: "小红书", emoji: "📕",
    audience: "一二线城市年轻女性与家居美学人群，重视真实体验与颜值",
    acquisition_plays: ["达人种草与合集", "笔记评论区承接咨询", "专业号私域沉淀"],
    best_practices: ["3:4 封面 + 20 字内标题", "正文讲场景与避坑细节", "话题标签 5–8 个"],
    content_formats: ["图文笔记", "视频笔记"],
    publish_mode: "manual_export",
    compliance_note: "通用营销 SaaS 无 write_notes 权限：导出成片与文案后需在官方创作中心人工确认发布，禁止私信群控外挂。",
  },
  {
    platform: "douyin", region: "domestic", label: "抖音", emoji: "🎬",
    audience: "国内下沉与年轻消费人群，直播与短视频成交效率高",
    acquisition_plays: ["工厂实拍短视频", "直播讲解与团购", "评论区问题承接"],
    best_practices: ["前 3 秒展示成品效果", "竖屏原生字幕", "挂车与内容主题一致"],
    content_formats: ["短视频", "直播"],
    publish_mode: "manual_export",
    compliance_note: "应用私信权限已回收且 h5.share 禁止商品营销类应用：仅提供成片打包与人工发布指引。",
  },
  {
    platform: "kuaishou", region: "domestic", label: "快手", emoji: "⚡",
    audience: "国内信任型电商人群与产业带买家，老铁经济转化稳定",
    acquisition_plays: ["产业带溯源短视频", "直播带货", "粉丝群复购运营"],
    best_practices: ["真人出镜讲工艺", "直播前预热短视频", "售后响应写进话术"],
    content_formats: ["短视频", "直播"],
    publish_mode: "manual_export",
    compliance_note: "无通用第三方发布接口，走成片导出与官方后台人工发布。",
  },
  {
    platform: "bilibili", region: "domestic", label: "B站", emoji: "🅱️",
    audience: "国内年轻技术爱好者与测评受众，适合深度参数解读",
    acquisition_plays: ["UP 主测评合作", "技术向长视频", "专栏图文沉淀搜索"],
    best_practices: ["封面信息层级清晰", "分 P 讲清安装与参数", "评论区置顶选型指引"],
    content_formats: ["中长视频", "专栏", "动态"],
    publish_mode: "manual_export",
    compliance_note: "无通用自动发布接口，需人工上传并遵守社区规范。",
  },
  {
    platform: "wechat_video", region: "domestic", label: "微信视频号", emoji: "📹",
    audience: "国内微信生态私域客户与经销商，熟人关系链传播",
    acquisition_plays: ["视频号内容 + 企业微信承接", "直播预约沉淀", "公众号互导"],
    best_practices: ["竖屏 9:16", "口播给结论", "直播间挂载企业微信入口"],
    content_formats: ["短视频", "直播"],
    publish_mode: "manual_export",
    compliance_note: "无第三方发布 API，需人工在视频号助手发布；绝不使用个人微信外挂。",
  },
  {
    platform: "wechat_oa", region: "domestic", label: "微信公众号", emoji: "📰",
    audience: "国内私域订阅用户与 B2B 经销商，适合深度内容与客服承接",
    acquisition_plays: ["图文深度内容", "菜单承接选型入口", "48 小时客服消息跟进"],
    best_practices: ["草稿与发布分离，人工确认终稿", "标题含搜索关键词", "客服消息严格在 48 小时窗口内"],
    content_formats: ["图文", "视频", "菜单"],
    publish_mode: "api",
    compliance_note: "仅通过官方第三方平台草稿/发布接口操作，客服消息受限 48 小时窗口与条数限制。",
  },
  {
    platform: "weibo", region: "domestic", label: "微博", emoji: "🌐",
    audience: "国内公共话题与品牌声量人群，适合活动与新品曝光",
    acquisition_plays: ["话题活动", "超话与粉丝群运营", "KOL 转发"],
    best_practices: ["话题词统一", "配图含品牌信息", "舆情及时响应"],
    content_formats: ["图文", "视频", "话题"],
    publish_mode: "manual_export",
    compliance_note: "发布走官方后台人工确认，舆情监听仅采集公开内容。",
  },
];

export const PLAYBOOK_BY_PLATFORM: Readonly<Record<SocialPlatform, PlatformPlaybook>> =
  Object.fromEntries(PLATFORM_PLAYBOOKS.map((item) => [item.platform, item])) as Record<SocialPlatform, PlatformPlaybook>;

export function getPlaybook(platform: SocialPlatform): PlatformPlaybook {
  return PLAYBOOK_BY_PLATFORM[platform];
}
