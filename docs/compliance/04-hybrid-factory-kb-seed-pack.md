# Hybrid Factory Knowledge Base Seed Pack — Demo Tenant `quanzhou-led-factory`

**Purpose:** A ready-to-ingest, bilingual (English / 中文) knowledge base for the demo tenant **Quanzhou Smart LED Factory** (`quanzhou-led-factory`). It is the corpus a demo tenant ingests so that specification, certification, warranty, and logistics questions are answerable from an inline citation instead of from the model's memory. The 100-case evaluation fixture (`apps/backend/tests/fixtures/hybrid_factory_eval_100.json`) is a **separate** deterministic preset (`quanzhou_smart_led_factory`) that uses its own legacy demo model codes (`XJ-*`) and asserts harness gates against seeded rows, not against this pack; when the demo data is rebranded, update both on purpose rather than assuming they are the same corpus.

**Ingestion contract:** each section below is one document. Split on the `## DOC` headings; store the fenced `yaml` block as document metadata, and the body (from the `### English` heading onward) as the chunk source. Expected result: 8 documents, each retrievable by `doc_id` and citable inline as `[qz-kb-00N#chunk_id]`.

**Hard constraint — no live numbers in the knowledge base:** no unit price, discount, MOQ tier price, exchange rate, stock quantity, in-transit quantity, order status, or shipment event appears anywhere in this pack. The retrieval layer must refuse to answer those questions from documents and instead call the deterministic ERP tools. Section 8 states the answering policy that the runtime enforces.

**Status:** demo seed content. Replace with the factory's controlled documents (signed spec sheets, test reports, ISO certificates) before a production tenant ingests it. Statements of certification describe the certificates the factory holds; certificate numbers and lab reports are provided on request and are not reproduced here.

---

## DOC 1 — `qz-kb-001` · Company profile, catalogue map, and knowledge-base scope

```yaml
doc_id: qz-kb-001
title: Company Profile, Catalogue Map, and Knowledge-Base Scope
title_zh: 公司概况、产品目录与知识库范围
tenant: quanzhou-led-factory
language: en+zh
doc_type: company_profile
products: [LED-STRIP-5M-IP65, LED-DRIVER-24V-100W, LED-FLOOD-400-EU, LED-HIGHBAY-200-US, LED-SOLAR-150-IP67]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

Quanzhou Smart LED Factory is an LED lighting manufacturer and exporter based in Quanzhou, Fujian, China. We design, assemble, and test LED strip, drivers, floodlights, high-bay luminaires, and solar street lights for two customer types: retail and e-commerce buyers who need small, fast shipments, and OEM/ODM business buyers who need customised specifications, private labelling, and volume production.

Catalogue map:

| SKU family | What it is | Typical use | Primary markets |
| :--- | :--- | :--- | :--- |
| `LED-STRIP-5M-IP65` | 5-metre flexible LED strip, IP65 protected, cuttable, 24V | Architectural cove, retail display, signage | EU, UK, US, AU |
| `LED-DRIVER-24V-100W` | 24V constant-voltage driver, 100W class, IP67 potted option | Powers strip and low-voltage fixtures | EU, UK, US |
| `LED-FLOOD-400-EU` | 400W-class stadium and area floodlight | Sports fields, yards, industrial yards | EU, UK, Middle East |
| `LED-HIGHBAY-200-US` | 200W-class linear high bay | Warehouses, workshops, gyms | US, CA |
| `LED-SOLAR-150-IP67` | 150W-class solar street light with integrated battery | Roads, campuses, off-grid sites | SEA, Africa, LATAM |

What this knowledge base answers: product specifications, materials, certifications, ingress protection, dimming and control compatibility, packaging and incoterms, OEM/ODM process and lead times, warranty, returns and RMA procedure, and installation or handling guidance.

What this knowledge base does not answer, by design: current unit price, tiered or negotiated price, MOQ price breaks, live stock or available-to-promise quantity, production slot availability, order status, tracking events, and settlement figures. Those are operational facts that change continuously; they are answered only by live queries against the factory's ERP, and any answer that quotes such a number without a live data reference is treated as a defect.

### 中文

泉州智能 LED 工厂位于中国福建泉州，是一家 LED 照明制造与出口企业。我们设计、组装并检测 LED 灯带、驱动电源、投光灯、工矿灯与太阳能路灯，服务两类客户：需要小批量快速发货的零售与电商买家，以及需要定制规格、自有品牌与批量生产的 OEM/ODM 贸易买家。

产品目录：

| SKU 系列 | 产品定义 | 典型用途 | 主要市场 |
| :--- | :--- | :--- | :--- |
| `LED-STRIP-5M-IP65` | 5 米柔性 LED 灯带，IP65 防护，可裁剪，24V | 建筑灯槽、店铺陈列、标识 | 欧盟、英国、美国、澳洲 |
| `LED-DRIVER-24V-100W` | 24V 恒压驱动电源，100W 等级，可选 IP67 灌封 | 为灯带与低压灯具供电 | 欧盟、英国、美国 |
| `LED-FLOOD-400-EU` | 400W 等级体育场与区域投光灯 | 球场、堆场、工业场地 | 欧盟、英国、中东 |
| `LED-HIGHBAY-200-US` | 200W 等级线性工矿灯 | 仓库、车间、体育馆 | 美国、加拿大 |
| `LED-SOLAR-150-IP67` | 150W 等级一体化太阳能路灯 | 道路、园区、离网场景 | 东南亚、非洲、拉美 |

本知识库回答：产品规格、材料、认证、防护等级、调光与控制兼容性、包装与贸易术语、OEM/ODM 流程与交期、保修、退换货与 RMA 流程，以及安装与使用注意事项。

本知识库按设计**不回答**：当前单价、阶梯价与议价、MOQ 价格档、实时库存与可承诺数量、产线排期、订单状态、物流轨迹与结算金额。这些属于持续变化的运营数据，只能通过工厂 ERP 的实时查询回答；任何未附带实时数据引用的此类数字都视为缺陷。

---

## DOC 2 — `qz-kb-002` · `LED-STRIP-5M-IP65` specification

```yaml
doc_id: qz-kb-002
title: LED-STRIP-5M-IP65 Product Specification
title_zh: LED-STRIP-5M-IP65 产品规格
tenant: quanzhou-led-factory
language: en+zh
doc_type: product_spec
products: [LED-STRIP-5M-IP65]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

`LED-STRIP-5M-IP65` is a 5-metre flexible LED strip on a white FPC carrier, supplied on 8 mm width for standard profiles and 10 mm for high-density variants.

Electrical: 24V DC constant-voltage input; the strip is powered by `LED-DRIVER-24V-100W` or an equivalent certified 24V constant-voltage driver. It must never be connected directly to mains voltage. Power is fed from both ends on runs longer than 5 metres to limit voltage drop; a single continuous run must not exceed 10 metres without a re-feed. Cutting is permitted only at the marked cut points, in multiples of the marked segment, and the cut end must be re-sealed with the supplied end cap and neutral-cure silicone before use outdoors.

Optical: available in 2700K, 3000K, 4000K, and 6500K correlated colour temperature, with a colour rendering index of Ra>80 as standard and Ra>90 on request. Binning is within a 3-step MacAdam ellipse. Dimming is supported by PWM on the 24V side and is compatible with TRIAC, 0-10V, and DALI-2 dimmers when paired with the matching driver; the strip itself contains no dimming electronics.

Construction and protection: 2 oz copper FPC, 3M adhesive backing for indoor mounting, and a silicone-coated or silicone-tube construction for the IP65 rating. IP65 means protection against dust ingress and against water jets from any direction; it is not submersible and must not be installed in a location where water can pool. For submersible or continuous-immersion installations, use the IP67 variant of the strip and seal every joint with a potted connector.

Thermal: the strip must be mounted on an aluminium profile or other thermally conductive surface when operated above 60% of rated power, and must not be run inside a sealed enclosure without a thermal path. Ambient operating range is -20 °C to +45 °C; storage range is -30 °C to +60 °C.

Lifetime: L70 at 25,000 hours when mounted on a suitable heat sink at rated current and an ambient of 25 °C, based on LM-80 data for the LED package. Warranty terms are in `qz-kb-007`.

Compliance: CE and RoHS declarations are held for the EU; UKCA documentation is available for the UK; the LED package and FPC are REACH-compliant. See `qz-kb-004` for the certification matrix and `qz-kb-005` for the IP test basis.

### 中文

`LED-STRIP-5M-IP65` 为 5 米柔性 LED 灯带，白色 FPC 基板，标准款宽 8 mm，高密度款宽 10 mm。

电气：24V DC 恒压输入，须由 `LED-DRIVER-24V-100W` 或同等认证的 24V 恒压驱动电源供电，严禁直接接入市电。超过 5 米的灯带须双端供电以抑制压降；单段连续长度不得超过 10 米（不含补电）。仅允许在标记的裁剪点按段倍数裁剪，裁剪端必须使用随附尾盖与中性固化硅胶重新密封后方可户外使用。

光学：色温可选 2700K、3000K、4000K、6500K；显色指数标准 Ra>80，可定制 Ra>90；分 bin 控制在 3 阶麦克亚当椭圆内。调光在 24V 侧采用 PWM，配合对应驱动电源可兼容 TRIAC、0-10V 与 DALI-2 调光器；灯带本体不含调光电路。

结构与防护：2 oz 铜 FPC，室内安装配 3M 背胶；IP65 等级采用硅胶涂层或硅胶套管结构。IP65 表示防尘且可承受任意方向喷水，**不可浸水**，不得安装在会积水的部位。需要浸水或长期水下使用时，应改用 IP67 灯带并对每个接头做灌封处理。

散热：在额定功率 60% 以上运行时必须安装在铝型材或其他导热基面上；不得在无散热路径的密闭腔体内使用。工作环境温度 -20 ℃ 至 +45 ℃，储存温度 -30 ℃ 至 +60 ℃。

寿命：在适配散热基面、额定电流、25 ℃ 环境条件下，基于 LED 封装 LM-80 数据，L70 寿命 25,000 小时。保修条款见 `qz-kb-007`。

合规：持有欧盟 CE 与 RoHS 声明，英国 UKCA 文件可提供；LED 封装与 FPC 符合 REACH。认证矩阵见 `qz-kb-004`，防护等级测试依据见 `qz-kb-005`。

---

## DOC 3 — `qz-kb-003` · `LED-DRIVER-24V-100W` specification

```yaml
doc_id: qz-kb-003
title: LED-DRIVER-24V-100W Product Specification
title_zh: LED-DRIVER-24V-100W 产品规格
tenant: quanzhou-led-factory
language: en+zh
doc_type: product_spec
products: [LED-DRIVER-24V-100W]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

`LED-DRIVER-24V-100W` is a constant-voltage LED driver delivering 24V DC at a 100W class rating, intended to power LED strip and other 24V low-voltage luminaires.

Input: 100–277V AC, 50/60 Hz, with active power-factor correction (PF>0.95 at full load). The wide input range covers both 120V North American and 230V European/UK supplies in a single model, which simplifies spares holding for distributors.

Output: 24V DC constant voltage with ±5% tolerance, ripple and noise within the limits stated in the datasheet, and current limiting plus short-circuit, over-load, over-temperature, and open-circuit protection. The driver is Class II (double-insulated) and does not require a protective earth bond; where the enclosure is metal, the installer must still bond the enclosure to earth per local wiring rules.

Surge and isolation: surge protection is 6 kV common-mode / 10 kV differential-mode with an external SPD recommended on exposed outdoor circuits. Input-to-output isolation meets reinforced-insulation requirements for SELV output, and the output is safety-extra-low-voltage, so the 24V side may be worked on without mains hazard once the input is isolated.

Thermal and construction: aluminium housing with a thermal potting option; the standard version is IP20 for indoor cabinet mounting and the potted version is IP67 for outdoor and wet locations. Operating ambient is -25 °C to +50 °C with derating above +40 °C; the case must not exceed the temperature marked on the label. Do not cover the driver or install it in a sealed box without ventilation unless the thermal calculation has been done for that enclosure.

Dimming and control: 1-10V, 0-10V, TRIAC/phase-cut, and DALI-2 dimming variants are available; the dimming variant must be ordered explicitly, because the base model is non-dimmable. Do not connect a phase-cut dimmer to a non-dimmable variant.

Compliance and warranty: CE, RoHS, and UKCA documentation; UL/cUL recognised component on the US variant; see `qz-kb-004`. Warranty is covered in `qz-kb-007`; the driver carries a 5-year limited warranty, and the warranty is void if the driver is operated above its rated load or in an enclosure that exceeds its ambient rating.

### 中文

`LED-DRIVER-24V-100W` 为恒压 LED 驱动电源，输出 24V DC，功率等级 100W，用于为 LED 灯带及其他 24V 低压灯具供电。

输入：100–277V AC，50/60 Hz，带主动功率因数校正（满载 PF>0.95）。宽压输入使同一型号可覆盖北美 120V 与欧洲/英国 230V 电网，便于经销商备件管理。

输出：24V DC 恒压，容差 ±5%，纹波噪声以规格书为准；具备限流、短路、过载、过温与开路保护。产品为 Class II 双重绝缘，无需保护接地；若外壳为金属，安装方仍须按当地电气规范将外壳接地。

浪涌与隔离：共模 6 kV / 差模 10 kV 浪涌防护；户外裸露线路建议外加 SPD。输入对输出满足 SELV 加强绝缘要求，输出为安全特低电压，断开输入后 24V 侧不带市电危险电压。

散热与结构：铝外壳，可选导热灌封；标准款 IP20 用于室内机柜安装，灌封款 IP67 用于户外与潮湿场所。工作环境温度 -25 ℃ 至 +50 ℃，超过 +40 ℃ 需降额使用；外壳温度不得超过标签标示值。不得覆盖驱动器，也不得在未做该箱体热计算的情况下装入密闭箱体。

调光与控制：可选 1-10V、0-10V、TRIAC 切相与 DALI-2 调光版本；基础型号不带调光，调光款须明确下单。禁止将切相调光器接入非调光型号。

合规与保修：提供 CE、RoHS 与 UKCA 文件；美规型号具 UL/cUL 认可元件。详见 `qz-kb-004`。保修见 `qz-kb-007`：驱动器享 5 年有限保修；超出额定负载运行或在外壳环境温度超限条件下使用将导致保修失效。

---

## DOC 4 — `qz-kb-004` · Certification matrix (CE / FCC / RoHS / UL / UKCA)

```yaml
doc_id: qz-kb-004
title: Certification Matrix (CE, FCC, RoHS, UL, UKCA)
title_zh: 认证矩阵（CE / FCC / RoHS / UL / UKCA）
tenant: quanzhou-led-factory
language: en+zh
doc_type: certification
products: [LED-STRIP-5M-IP65, LED-DRIVER-24V-100W, LED-FLOOD-400-EU, LED-HIGHBAY-200-US, LED-SOLAR-150-IP67]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

| SKU family | CE (EU) | UKCA (UK) | RoHS | FCC | UL / ETL | Other |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `LED-STRIP-5M-IP65` | Yes — LVD + EMC | Available | Yes | Class B on the 24V accessory set | Optional UL-listed FPC and wire | REACH, EN 62471 photobiological |
| `LED-DRIVER-24V-100W` | Yes — LVD + EMC + ErP | Available | Yes | Class B | UL/cUL recognised component (US variant) | SELV output, EN 61347 |
| `LED-FLOOD-400-EU` | Yes — LVD + EMC | Available | Yes | Class B | Optional | ENEC on request, EN 60598, IK08 |
| `LED-HIGHBAY-200-US` | Yes — LVD + EMC | Available | Yes | Class B | UL and DLC listed | EN 60598, 0-10V / DALI-2 |
| `LED-SOLAR-150-IP67` | Yes — LVD + EMC | Available | Yes | Class B | Optional | UN 38.3 battery transport report, IEC 62133 cells |

How to read this matrix: "Yes" means a current declaration of conformity is held and can be supplied as a PDF with the shipment or on request. "Available" means the documentation exists for that market but the labelling and declaration must be requested per order so the correct mark is applied at the factory. "Optional" means the listing is not part of the standard build and must be quoted and scheduled as an option.

What the certificates cover and do not cover: the declaration covers the product as shipped with the factory-supplied driver and accessories. Replacing the driver with an uncertified unit, modifying the wiring, or combining the luminaire with a third-party control gear invalidates the EMC and safety basis of the declaration, and the resulting assembly is the modifier's responsibility.

Documents provided with a certificate pack: declaration of conformity, test summary, photobiological safety report where applicable, RoHS/REACH declarations for the bill of materials, and the safety data sheet for the battery where a battery is included. Certificate numbers and full laboratory reports are released to buyers and to customs brokers on request; they are not published in this knowledge base.

Customs and labelling: HS classification and origin marking are provided per shipment on the commercial invoice and packing list. The factory does not provide legal advice on import classification; the importer of record remains responsible for the final classification in the destination country.

### 中文

| SKU 系列 | CE（欧盟） | UKCA（英国） | RoHS | FCC | UL / ETL | 其他 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `LED-STRIP-5M-IP65` | 有 — LVD + EMC | 可提供 | 有 | 24V 配件组 Class B | 可选 UL 认证 FPC 与线材 | REACH、EN 62471 光生物安全 |
| `LED-DRIVER-24V-100W` | 有 — LVD + EMC + ErP | 可提供 | 有 | Class B | 美规型号 UL/cUL 认可元件 | SELV 输出、EN 61347 |
| `LED-FLOOD-400-EU` | 有 — LVD + EMC | 可提供 | 有 | Class B | 可选 | 可按需提供 ENEC、EN 60598、IK08 |
| `LED-HIGHBAY-200-US` | 有 — LVD + EMC | 可提供 | 有 | Class B | UL 与 DLC 列名 | EN 60598、0-10V / DALI-2 |
| `LED-SOLAR-150-IP67` | 有 — LVD + EMC | 可提供 | 有 | Class B | 可选 | UN 38.3 电池运输报告、IEC 62133 电芯 |

如何理解本矩阵：“有”表示持有现行符合性声明，可随货或按需提供 PDF。“可提供”表示该市场文件齐备，但须按订单单独申请，以便工厂施加正确标志。“可选”表示该列名不属于标准配置，需单独报价与排期。

证书的覆盖范围与例外：符合性声明覆盖按出厂配置发货的产品（含工厂配套驱动与配件）。若替换为未认证驱动、改动接线，或将灯具与第三方控制装置组合，则该声明的 EMC 与安全基础失效，组合体责任由改动方承担。

证书包随附文件：符合性声明、测试摘要、适用时的光生物安全报告、物料清单的 RoHS/REACH 声明，以及含电池产品的电池安全数据表。证书编号与完整实验室报告可向买家与报关行提供，不在本知识库中公开。

海关与标识：HS 归类与原产地标识按票提供于商业发票与装箱单。工厂不就进口归类提供法律意见，最终归类责任由目的国进口方承担。

---

## DOC 5 — `qz-kb-005` · Ingress protection: IP65, IP67, and IK ratings

```yaml
doc_id: qz-kb-005
title: Ingress Protection — IP65, IP67, and IK Impact Ratings
title_zh: 防护等级 — IP65、IP67 与 IK 抗冲击等级
tenant: quanzhou-led-factory
language: en+zh
doc_type: technical_note
products: [LED-STRIP-5M-IP65, LED-DRIVER-24V-100W, LED-FLOOD-400-EU, LED-HIGHBAY-200-US, LED-SOLAR-150-IP67]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

IP ratings have two digits. The first digit is solids (dust), the second is liquids (water). `6` for solids means dust-tight: no dust ingress at all. The second digit is where buyers most often misread the specification:

| Rating | Water condition | What it means in practice | What it does not mean |
| :--- | :--- | :--- | :--- |
| IP65 | Water jets from any direction, 6.3 mm nozzle | Suitable for outdoor mounting under eaves, washdown of equipment from a hose at low pressure, and rain with wind | Not submersible; not for standing water or buried junction boxes |
| IP66 | Powerful water jets, 12.5 mm nozzle | Suitable for exposed industrial washdown | Not submersible |
| IP67 | Temporary immersion, 1 m for 30 minutes | Suitable for outdoor fixtures that may sit in a puddle, and for potted drivers in wet locations | Not for continuous immersion; not for high-pressure jet cleaning |
| IP68 | Continuous immersion per the manufacturer's declared depth and time | Only where the factory has declared the depth and duration for that specific model | Not a generic claim; do not assume IP68 for any model without the declared rating |

`LED-STRIP-5M-IP65` is rated IP65 and is not submersible; every cut end, connector, and solder joint must be sealed to keep that rating. `LED-DRIVER-24V-100W` standard is IP20 for indoor cabinets and the potted variant is IP67. `LED-SOLAR-150-IP67` is IP67 with a sealed battery compartment. `LED-FLOOD-400-EU` and `LED-HIGHBAY-200-US` are IP66 for the optical and driver compartments.

Impact: IK08 means resistance to a 5-joule impact (roughly a 1.7 kg object dropped from 25 cm) and is the rating held by the floodlight and high-bay families. IK08 is not a vandal-resistance claim and does not cover deliberate damage or impacts on the mounting bracket.

Test basis: IP ratings are verified to IEC 60529 by an accredited laboratory, and IK ratings to IEC 62262. Statements in this document summarise the ratings; they do not replace the laboratory reports, which are available on request.

Installation rules that preserve the rating: use the supplied gaskets and torque the screws evenly; do not drill additional cable entries without a matching gland; use neutral-cure silicone, never acid-cure; leave a drip loop on outdoor cable entries; and never seal a fixture so tightly that condensation cannot escape unless a breather valve is fitted.

### 中文

IP 等级由两位数字组成：第一位为固体（粉尘）防护，第二位为液体（水）防护。固体等级 `6` 表示尘密，完全无粉尘进入；买家最容易误读的是第二位数字：

| 等级 | 水条件 | 实际含义 | 不意味着 |
| :--- | :--- | :--- | :--- |
| IP65 | 任意方向喷水，6.3 mm 喷嘴 | 适用于檐下户外安装、低压水枪冲洗设备、风雨环境 | 不可浸水；不适用于积水处或埋地接线盒 |
| IP66 | 强力喷水，12.5 mm 喷嘴 | 适用于暴露的工业冲洗环境 | 不可浸水 |
| IP67 | 短时浸水，1 米 30 分钟 | 适用于可能泡在浅积水中的户外灯具与潮湿场所灌封驱动 | 不可长期浸水；不可高压射流清洗 |
| IP68 | 按厂家声明的深度与时间持续浸水 | 仅限工厂已针对该型号声明深度与时长的情况 | 不是通用声明；未经声明不得假定任何型号为 IP68 |

`LED-STRIP-5M-IP65` 为 IP65，不可浸水；每个裁剪端、接头与焊点都必须密封才能维持该等级。`LED-DRIVER-24V-100W` 标准款 IP20（室内机柜），灌封款 IP67。`LED-SOLAR-150-IP67` 为 IP67，电池仓密封。`LED-FLOOD-400-EU` 与 `LED-HIGHBAY-200-US` 的光学腔与驱动腔为 IP66。

抗冲击：IK08 表示可承受 5 焦耳冲击（约 1.7 kg 物体从 25 cm 高度落下），为投光灯与工矿灯系列所持等级。IK08 不是防破坏声明，不覆盖人为破坏或对安装支架的冲击。

测试依据：IP 等级由认可实验室按 IEC 60529 验证，IK 等级按 IEC 62262 验证。本文为等级说明摘要，不替代实验室报告；报告可按需提供。

维持等级的安装要求：使用随附密封圈并均匀施力拧紧螺钉；不得在无匹配格兰头的情况下另行开孔进线；使用中性固化硅胶，严禁酸性胶；户外进线须留滴水弯；除安装呼吸阀外，不得将有冷凝风险的灯具完全密封。

---

## DOC 6 — `qz-kb-006` · OEM/ODM customisation and lead times

```yaml
doc_id: qz-kb-006
title: OEM/ODM Customisation and Lead Times
title_zh: OEM/ODM 定制与交期
tenant: quanzhou-led-factory
language: en+zh
doc_type: process_policy
products: [LED-STRIP-5M-IP65, LED-DRIVER-24V-100W, LED-FLOOD-400-EU, LED-HIGHBAY-200-US, LED-SOLAR-150-IP67]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

We accept OEM (your brand on our standard product) and ODM (a modified or newly developed product) programmes. Minimum order quantities, tooling costs, and unit prices depend on the SKU, the modification, and the order volume; they are quoted per project and are never stated in this knowledge base.

What can be customised: colour temperature and CRI binning; beam angle and lens optics; housing colour and finish; cable length, connector type, and plug type per destination; label artwork, manual language, and gift-box or retail packaging; driver brand and dimming protocol; solar panel and battery capacity within the enclosure's thermal limits; and firmware behaviour for sensor and dimming profiles on the solar family.

What cannot be customised: the safety-critical construction that underpins the certificate (for example, removing the double insulation on the driver, or changing the IP sealing method without re-testing), and any request that would require a certification mark the factory does not hold for that configuration. In those cases we either decline or quote a new certification project with its own timeline and cost.

Process and indicative lead times (working days after the stated trigger, excluding Chinese public holidays):

| Stage | Trigger | Indicative duration |
| :--- | :--- | :--- |
| Requirement review and feasibility | RFQ with target spec, volume, destination market | 2–4 working days |
| Quotation and commercial terms | Feasibility confirmed | 2–3 working days |
| Sample build (standard configuration) | Sample order and artwork approved | 7–10 working days |
| Sample build (new tooling or new certification) | Tooling deposit received, drawings approved | 20–35 working days |
| Buyer sample approval | Sample delivered | Buyer-dependent; 5 working days is typical |
| Bulk production (standard SKU) | Deposit received and artwork approved | 20–30 working days |
| Bulk production (customised SKU) | Deposit received and all approvals signed | 25–40 working days |
| Pre-shipment inspection | Production complete | 2–3 working days |
| Booking and loading | Inspection passed | 3–7 working days, subject to carrier space |

These are planning ranges, not commitments. A binding date is stated only in the proforma invoice or the sales contract for that order. Peak season (August to October, ahead of the European and North American retail season) and Chinese New Year (production stops for roughly two to three weeks around the holiday) extend these ranges; plan around them rather than assuming a standard cycle.

Buyer responsibilities: provide final artwork in vector format, confirm the destination market and plug/voltage requirement, confirm the certification marks required, and nominate the inspection regime (factory self-inspection, third-party inspection, or none). Changes after artwork approval restart the sample or production clock and may incur re-work charges.

### 中文

我们承接 OEM（在标准产品上使用客户品牌）与 ODM（改型或全新开发）项目。起订量、模具费与单价取决于 SKU、改动内容与订单量，按项目单独报价，本知识库不作任何价格陈述。

可定制项：色温与显色指数分 bin；光束角与透镜配光；外壳颜色与表面处理；线长、连接器类型与目的地插头；标签图案、说明书语言与彩盒/零售包装；驱动品牌与调光协议；在壳体热限内的太阳能板与电池容量；太阳能系列的传感器与调光固件行为。

不可定制项：支撑证书的安全关键结构（例如取消驱动双重绝缘，或在未重新测试的情况下改变 IP 密封方式），以及需要工厂并未持有的认证标志的配置。此类需求我们或拒绝，或按新的认证项目单独报价并给出独立周期与费用。

流程与参考交期（自所述触发点起的工作日，不含中国法定节假日）：

| 阶段 | 触发条件 | 参考时长 |
| :--- | :--- | :--- |
| 需求评审与可行性 | 收到含目标规格、数量、目的市场的 RFQ | 2–4 个工作日 |
| 报价与商务条款 | 可行性确认 | 2–3 个工作日 |
| 样品制作（标准配置） | 样品订单与图稿确认 | 7–10 个工作日 |
| 样品制作（新模具或新认证） | 收到模具定金、图纸确认 | 20–35 个工作日 |
| 买家样品确认 | 样品送达 | 取决于买家，通常 5 个工作日 |
| 批量生产（标准 SKU） | 收到定金、图稿确认 | 20–30 个工作日 |
| 批量生产（定制 SKU） | 收到定金、全部确认件签署 | 25–40 个工作日 |
| 出货前检验 | 生产完成 | 2–3 个工作日 |
| 订舱与装柜 | 检验通过 | 3–7 个工作日，取决于船公司舱位 |

上述为计划区间，不构成承诺。具有约束力的日期仅以该订单的形式发票或销售合同为准。旺季（8–10 月，欧美零售季前）与春节（假期前后约 2–3 周停产）会延长上述区间，请据此排期，不要按标准周期假设。

买家责任：提供矢量格式的最终图稿；确认目的市场与插头/电压要求；确认所需认证标志；指定检验方式（工厂自检、第三方验货或不验货）。图稿确认后的变更将重新计时并可能产生返工费用。

---

## DOC 7 — `qz-kb-007` · Warranty, returns, and RMA procedure

```yaml
doc_id: qz-kb-007
title: Warranty, Returns, and RMA Procedure
title_zh: 保修、退换货与 RMA 流程
tenant: quanzhou-led-factory
language: en+zh
doc_type: policy
products: [LED-STRIP-5M-IP65, LED-DRIVER-24V-100W, LED-FLOOD-400-EU, LED-HIGHBAY-200-US, LED-SOLAR-150-IP67]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

Warranty periods, counted from the shipment date on the commercial invoice: LED strip, floodlight, and high-bay families 5 years; LED driver 5 years; solar street light luminaire 5 years with a separate 3-year warranty on the battery. These periods apply to the product as shipped and installed per the published instructions.

What is covered: manufacturing defects in materials and workmanship; LED lumen maintenance below the L70 curve for the rated life; driver failure under rated load and rated ambient; delamination or yellowing of the diffuser caused by a material defect rather than by UV exposure beyond the rated environment; and IP integrity failure where the fixture was installed with the supplied gaskets and glands.

What is not covered: damage from incorrect wiring, over-voltage, lightning without an external SPD on an exposed circuit, or operation above rated load or rated ambient; water ingress through a field-drilled hole, an unsealed cut end on strip, or a missing gland; physical damage, vandalism, or damage in transit after the incoterm transfer point; cosmetic wear, colour shift within the declared binning tolerance, or flicker caused by a third-party dimmer that is not on the compatibility list; and products that have been repaired or modified by anyone other than the factory.

Retail returns (B2C): the consumer-facing return window and consumer rights are set by the seller of record and by local law, not by this document. The default window offered on the demo storefront is 30 days from delivery for unopened goods in resaleable condition, with the buyer paying return freight unless the item was faulty; this default is a commercial policy that the seller of record may lengthen or shorten, and the seller's published policy governs. Our factory warranty to the seller is independent of the seller's consumer return policy. Where the seller is the merchant of record, the seller answers the consumer and we support the seller through the RMA process below.

RMA process, step by step:
1. The buyer submits an RMA request with the invoice number, SKU, batch or date code, quantity, a description of the fault, and photographs or a short video showing the failure and the installation.
2. We acknowledge within 2 business days and may ask for additional evidence, including a driver output voltage measurement or a photo of the labels and the mounting.
3. We issue a disposition within 5 business days: replace, credit, repair, or reject with a technical reason.
4. For approved claims, we ship replacement units with the next production run or by express when the buyer requests and accepts the freight cost. Defective units are returned only when we request it; low-value units are usually scrapped locally with photo evidence to avoid freight cost.
5. Replacement units carry the remainder of the original warranty period, not a new period.
6. Every claim is logged with root cause. A claim pattern above the agreed quality threshold triggers a corrective-action report with containment, root cause, and preventive action.

Do not return goods before an RMA number is issued: shipments without an RMA number are refused at the factory and the freight cost is the sender's.

### 中文

保修期自商业发票上的出运日起算：灯带、投光灯与工矿灯系列 5 年；LED 驱动电源 5 年；太阳能路灯灯具 5 年，电池单独保修 3 年。上述期限适用于按公开说明安装与使用的出厂产品。

保修范围：材料与工艺的制造缺陷；在额定寿命内 LED 光通维持率低于 L70 曲线；在额定负载与额定环境温度下的驱动失效；因材料缺陷（而非超出额定环境的紫外线暴露）导致的扩散板脱层或发黄；以及在使用随附密封圈与格兰头安装情况下发生的防护等级失效。

不在保修范围：接线错误、过压、裸露线路未加 SPD 的雷击、超额定负载或超额定环境温度运行造成的损坏；经由现场开孔、灯带未密封裁剪端或缺失格兰头导致的进水；人为损坏、破坏行为，或在贸易术语风险转移点之后的运输损坏；外观磨损、在声明分 bin 容差内的色偏，或由不在兼容清单内的第三方调光器造成的闪烁；以及由非工厂方维修或改装的产品。

零售退货（B2C）：面向消费者的退货窗口与消费者权利由销售主体与当地法律确定，不由本文规定。演示店铺的默认政策为：交付后 30 天内、未拆封且可再销售的商品可退货，非质量问题由买家承担退货运费；该默认值为商务政策，销售主体可自行延长或缩短，以其公布的政策为准。工厂对卖家的保修独立于卖家的消费者退货政策。卖家作为销售主体时，由卖家答复消费者，我们通过下述 RMA 流程支持卖家。

RMA 流程：
1. 买家提交 RMA 申请，包含发票号、SKU、批次或日期码、数量、故障描述，以及能显示故障与安装状态的照片或短视频。
2. 我们在 2 个工作日内确认，并可能要求补充证据，包括驱动输出电压测量或标签与安装位置照片。
3. 我们在 5 个工作日内给出处理结论：换货、折让、维修，或附技术理由的拒赔。
4. 已批准的索赔，随下一生产批次发运替换品；买家要求且承担运费时可走快递。不良品仅在我们要求时退回；低价值产品通常凭照片证据就地报废，以避免运费。
5. 替换品沿用原保修期的剩余时间，不重新起算。
6. 每笔索赔均记录根本原因。索赔率超过约定质量阈值时，启动纠正措施报告，包含围堵、根因与预防措施。

未取得 RMA 号前请勿退货：无 RMA 号的到货将被工厂拒收，运费由发件方承担。

---

## DOC 8 — `qz-kb-008` · Packing, marking, FOB/DDP handover, and the live-data answering policy

```yaml
doc_id: qz-kb-008
title: Packing, Marking, FOB/DDP Handover, and Live-Data Answering Policy
title_zh: 包装、标识、FOB/DDP 交接与实时数据应答规则
tenant: quanzhou-led-factory
language: en+zh
doc_type: logistics_policy
products: [LED-STRIP-5M-IP65, LED-DRIVER-24V-100W, LED-FLOOD-400-EU, LED-HIGHBAY-200-US, LED-SOLAR-150-IP67]
effective_from: 2026-11-01
contains_live_pricing: false
contains_live_inventory: false
```

### English

Packing standards. LED strip ships on 5-metre reels in an anti-static bag with a desiccant, then in an individual carton with a cut-point diagram and a sealing kit. Drivers ship in an individual carton with foam end caps; the potted IP67 variant ships with the gland kit fitted and capped. Floodlights and high bays ship in a double-wall carton with a moulded pulp or EPE insert that protects the lens and the driver compartment; the mounting bracket is packed separately inside the same carton. Solar street lights ship as two cartons per set (luminaire and pole bracket, battery and panel) with the battery packed to UN 38.3 requirements and the terminals protected.

Carton marking, on two adjacent sides: SKU, description, quantity, gross and net weight, carton dimensions, batch or date code, country of origin, the certification marks held for the destination, the importer or consignee reference, and the handling symbols for fragile and keep-dry where applicable. Retail-ready packaging uses the buyer's artwork with the buyer's importer details; the factory mark stays on the outer carton only.

Palletising and container loading. Cartons are palletised at a maximum height that keeps the load stable and within the container's internal height after the pallet base; pallets are stretch-wrapped and corner-protected. Loading plans are issued per shipment with carton count, pallet count, and volume, and the load is photographed before the doors close. We load to the booked container size; over-declaring volume to "fill" a container is not done, because it distorts the buyer's landed-cost calculation.

Incoterms handover. Under FOB, our responsibility ends when the goods are loaded on board at the named port; the buyer arranges and pays ocean freight and insurance from that point, and the on-board bill of lading is the handover evidence. Under DDP, we arrange freight, import clearance, and duty to the named destination, and the buyer's only obligation is to receive the goods; DDP requires an accurate destination address, a valid importer of record, and a product that meets the destination's certification requirements, so DDP is quoted only after those three points are confirmed. CIF, CFR, EXW, and DAP are available as alternatives and are stated explicitly on the proforma invoice. The incoterm on the proforma invoice governs, not any statement in a chat message.

Documents issued per shipment: commercial invoice, packing list, bill of lading or airway bill, certificate of origin where required, the certificate pack from `qz-kb-004` where the destination requires it, and the battery transport documents for solar sets.

Live-data answering policy (binding on the AI assistant). Unit price, tiered price, MOQ price breaks, live stock, available-to-promise quantity, production slot availability, order status, tracking events, and settlement amounts are never answered from this knowledge base. When a buyer asks for any of them, the assistant must query the ERP through the deterministic tool layer and answer with the returned value and its tool-call reference; when the tool returns no data, the assistant must say so and route the request to a human, not estimate, average, or reuse a historical figure. Quotations above the tenant's autonomy threshold and any payment confirmation are created as proposals for human approval and are never sent by the assistant directly.

### 中文

包装标准。灯带以 5 米卷盘装于防静电袋并配干燥剂，再装入独立纸箱，随箱附裁剪点示意图与密封配件。驱动电源采用独立纸箱加泡棉端护；IP67 灌封款出厂即装好格兰头组件并加封盖。投光灯与工矿灯采用双瓦楞纸箱，配纸浆模塑或 EPE 内衬保护透镜与驱动腔；安装支架单独包装并置于同箱内。太阳能路灯按每套两箱发运（灯具与灯杆支架、电池与光伏板），电池按 UN 38.3 要求包装并做端子防护。

纸箱标识（两个相邻侧面）：SKU、品名、数量、毛重与净重、箱规、批次或日期码、原产国、目的市场所持认证标志、进口商或收货人参考号，以及适用时的易碎与防潮搬运标识。零售包装使用买家图稿与买家进口商信息，工厂标识仅保留在外箱。

托盘与装柜。纸箱码垛高度以装载稳定且扣除托盘后不超过柜内净高为限；托盘缠膜并加护角。每票出具装柜方案，列明箱数、托盘数与体积，并在关门拍照留档。我们按所订柜型装载，不做虚报体积“填柜”，以免扭曲买家的到岸成本核算。

贸易术语交接。FOB 项下，我方责任至货物在指定装运港装船为止；此后海运费与保险由买家安排并承担，已装船提单为交接凭证。DDP 项下，我方负责运输、进口清关与至指定目的地的关税，买家仅需收货；DDP 须先确认准确收货地址、有效进口主体，以及产品满足目的地认证要求，三项确认后方可报 DDP。也可选择 CIF、CFR、EXW 与 DAP，均须在形式发票上明确列示。以形式发票载明的贸易术语为准，聊天消息中的任何表述不构成变更。

每票随附单据：商业发票、装箱单、提单或空运单、需要的原产地证、目的国要求时附 `qz-kb-004` 所述证书包，以及太阳能套装的电池运输文件。

实时数据应答规则（对 AI 助手具有约束力）。单价、阶梯价、MOQ 价格档、实时库存、可承诺数量、产线排期、订单状态、物流轨迹与结算金额，一律不得依据本知识库回答。买家询问上述内容时，助手必须通过确定性工具层查询 ERP，并以返回值及其工具调用引用作答；工具无数据时必须如实说明并转人工，不得估算、取平均或复用历史数字。超过租户授权阈值的报价与任何付款确认，一律生成提案交人工审批，助手不得直接发送。
