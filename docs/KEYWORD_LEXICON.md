# 常用中英关键词词库

本文档整理当前项目里用于本地查询扩展的常用中英关键词词库。

- 代码来源: `backend/app/search_engine.py` 中 `_local_query_expansions()`
- 作用: 当用户输入中文、英文全称或常见缩写时，自动补充相关检索词，提高召回
- 说明: 这里列出的是当前已配置的主要映射关系；实际检索时会按代码里的双向扩展逻辑运行

## 疾病相关

| 主词 | 扩展词 |
| --- | --- |
| 白内障 | cataract, cataract surgery, congenital cataract |
| 近视 | myopia, high myopia |
| 高度近视 | high myopia |
| 青光眼 | glaucoma |
| 老年黄斑变性 | amd, age-related macular degeneration |
| AMD | 老年黄斑变性, age-related macular degeneration |
| 糖尿病视网膜病变 | diabetic retinopathy, 糖网, DR |
| 糖网 | diabetic retinopathy, 糖尿病视网膜病变, DR |
| DR | diabetic retinopathy, 糖尿病视网膜病变, 糖网 |
| 糖尿病黄斑水肿 | DME, diabetic macular edema |
| DME | 糖尿病黄斑水肿, diabetic macular edema |
| 视网膜静脉阻塞 | RVO, retinal vein occlusion |
| RVO | 视网膜静脉阻塞, retinal vein occlusion |
| 视网膜脱离 | RD, retinal detachment |
| RD | 视网膜脱离, retinal detachment |
| 视网膜病变 | retinopathy |
| 黄斑前膜 | ERM, epiretinal membrane |
| ERM | 黄斑前膜, epiretinal membrane |
| 黄斑裂孔 | MH, macular hole |
| MH | 黄斑裂孔, macular hole |
| 中心性浆液性脉络膜视网膜病变 | CSC, CSCR, central serous chorioretinopathy |
| CSC | 中心性浆液性脉络膜视网膜病变, CSCR, central serous chorioretinopathy |
| CSCR | 中心性浆液性脉络膜视网膜病变, CSC, central serous chorioretinopathy |
| 早产儿视网膜病变 | ROP, retinopathy of prematurity |
| ROP | 早产儿视网膜病变, retinopathy of prematurity |
| 原发性开角型青光眼 | POAG, primary open-angle glaucoma |
| POAG | 原发性开角型青光眼, primary open-angle glaucoma |
| 原发性闭角型青光眼 | PACG, primary angle-closure glaucoma |
| PACG | 原发性闭角型青光眼, primary angle-closure glaucoma |
| 干眼 | dry eye, dry eye disease, 干眼症, DED |
| 干眼症 | dry eye, dry eye disease, 干眼, DED |
| DED | 干眼, 干眼症, dry eye disease |
| 斜视 | strabismus |
| 弱视 | amblyopia |
| 圆锥角膜 | KC, keratoconus |
| KC | 圆锥角膜, keratoconus |
| 葡萄膜炎 | uveitis |
| 泪器病 | lacrimal disease, lacrimal, nasolacrimal, lacrimal duct |
| 泪器 | lacrimal, nasolacrimal, lacrimal duct |

## 检查相关

| 主词 | 扩展词 |
| --- | --- |
| OCT | 光学相干断层扫描, optical coherence tomography |
| 光学相干断层扫描 | OCT, optical coherence tomography |
| FFA | 荧光素眼底血管造影, fundus fluorescein angiography |
| 荧光素眼底血管造影 | FFA, fundus fluorescein angiography |
| ICG | 吲哚菁绿血管造影, indocyanine green angiography |
| ICGA | 吲哚菁绿血管造影, indocyanine green angiography |
| 吲哚菁绿血管造影 | ICG, ICGA, indocyanine green angiography |
| 眼压 | intraocular pressure |
| 眼底 | fundus |
| 视力 | visual acuity |

## 治疗、术式与器械

| 主词 | 扩展词 |
| --- | --- |
| 抗VEGF | anti-VEGF, vascular endothelial growth factor inhibitor |
| anti-VEGF | 抗VEGF, vascular endothelial growth factor inhibitor |
| 玻切 | 玻璃体切割术, vitrectomy, PPV |
| 玻璃体切割术 | 玻切, vitrectomy, PPV |
| vitrectomy | 玻切, 玻璃体切割术, PPV |
| PPV | 玻切, 玻璃体切割术, vitrectomy |
| 白内障超乳 | phaco, phacoemulsification |
| phaco | 白内障超乳, phacoemulsification |
| phacoemulsification | 白内障超乳, phaco |
| 人工晶体 | IOL, intraocular lens |
| IOL | 人工晶体, intraocular lens |
| 飞秒激光 | femtosecond laser, femto |
| femtosecond laser | 飞秒激光, femto |
| femto | 飞秒激光, femtosecond laser |
| SMILE | 全飞秒, small incision lenticule extraction |
| 全飞秒 | SMILE, small incision lenticule extraction |
| LASIK | 准分子激光原位角膜磨镶术, laser in situ keratomileusis |
| 准分子激光原位角膜磨镶术 | LASIK, laser in situ keratomileusis |

## 通用眼科相关词

| 主词 | 扩展词 |
| --- | --- |
| 视网膜 | retina, retinal |
| 角膜 | cornea, corneal |
| 黄斑 | macular |
| 屈光 | refractive, refraction |
| 人工智能 | artificial intelligence |

## 维护建议

- 新增术语时，尽量同时补充中文、英文全称、常用缩写
- 缩写如果歧义较大，最好配套中文主词一起映射，避免误召回
- 若后续词库继续变大，建议把这部分从代码里拆成单独的 JSON 或 YAML 配置文件，维护会更轻松
