import sys as _retired_sys
if "--run-retired-historical-script" not in _retired_sys.argv:
    raise SystemExit("已退役的历史脚本（0.3.1/0.4.3口径）：会改写验收台账或把整个validation目录打包到项目上级目录；默认拒绝运行。")
"""Maintain dispositions without recertifying historical research results."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import atomic_json,sha
root=Path(__file__).resolve().parents[1]
path=root/'validation/external_review_resolution.json';d=json.loads(path.read_text(encoding='utf-8'))
notes={
'P0-3':('DOCUMENTED','当前源码、README和当前状态统一0.4.3；目录是稳定入口别名，不是版本证据；旧证书保留真实版本。'),
'P0-4':('IMPLEMENTED_PENDING_FINAL_REGRESSION','输入审计迁移按内容哈希导入独立暂停任务，不依赖旧盘符；历史运行路径仅作来源记录。跨代码断点不能直接续跑。'),
'P0-6':('IMPLEMENTED_PENDING_FINAL_REGRESSION','源码发布白名单排除虚拟环境、工作区和历史验证目录；保留本机研究证据，不删除原任务。'),
'P1-2':('SCOPE_CLARIFIED','非standard保留旧档较弱的数值邻域；完整v3研究使用standard，两者不可混称标准验收。'),
'P1-3':('SCOPE_CLARIFIED','旧档零有效邻域保守淘汰；standard有完整邻域与回退。未将旧档结果冒充standard。'),
'P1-4':('SCOPE_CLARIFIED','旧档比较池明确标注仅合格代表；standard比较池包含全部候选家族。'),
'P1-11':('DOCUMENTED','机器规则与数据契约已同步FSL_signal_state_v4。'),
'P1-20':('IMPLEMENTED_PENDING_FINAL_REGRESSION','严格净胜>10对应整数upper<=threshold可剪枝；添加等号边界及非网格金额测试。'),
'P2-1':('DISPROVED','合法四分盘相邻半盘与整数比分不会同时半赢半输；全网格结算回归覆盖五种有效结局。'),
'P2-2':('DOCUMENTED','年度15场门槛按原价；压力各年样本和质量标记已导出，不暗中改变门槛。'),
'P2-9':('DOCUMENTED','契约分别描述旧档ID优先与standard冻结优先级。'),
'P2-10':('DOCUMENTED','80/15包含走盘；不含无效或跳过报价；已写入契约。'),
'P2-14':('RECOMMENDATION_REJECTED','缺源文件必须报错；已有文件fsync及15秒共享冲突重试。没有宣称Windows断电一致性保证。'),
'P3-5':('DOCUMENTED','去掉最多5笔盈利是有意压力定义，少于5笔时去掉全部；不修改为缺失值规避风险。'),
'P3-13':('DOCUMENTED','布尔图预算折半的额外索引余量已注释。'),
'P3-14':('RECOMMENDATION_REJECTED','保留内容SHA核验；大小和mtime不能证明内容未变，不引入未校验结果先运行的快路径。'),
'P3-16':('DOCUMENTED','当前README和启动入口使用-B。'),
'P3-17':('DOCUMENTED','安装文档区分开发版、支持范围、下载回退版及历史记录。'),
'P3-18':('DOCUMENTED','主令牌仅请求头/页面内存，下载使用短时一次性票据；文档已说明。'),
'P3-19':('DOCUMENTED','一字节锁用于协作互斥，不是文件访问控制；文档明确。'),
}
for item in d['items']:
    if item['id'] in notes:item['status'],item['resolution']=notes[item['id']]
d['state']='AWAITING_FROZEN_REGRESSION';atomic_json(path,d)
source=Path(r'C:\Users\Administrator\Desktop\检查出的问题.txt')
fixed=[('事件身份校验','校验重复ID与顺序在标准特征状态变更之前执行。'),('AH大盘口模板','增加绝对盘口3至3.5及至少3.5的强制登记。'),('回补异常','历史预验证，所有普通异常恢复内存；存储不确定后禁止继续及关闭写回。'),('24单位预览','按名单、情景、数值上限和组合政策匹配。'),('末场释放','三个验证引擎均释放最后一场。'),('缺失账本','只读打开，不创建空数据库；缺失表与文件记录证据缺口。'),('越界原子','未知及非整数ID直接拒绝。'),('窗口LRU','命中的兄弟窗口列刷新缓存顺序。'),('逐笔缓存','按解码JSON字节预算及128项限制；缓存命中也核对引用哈希。'),('滚动等待','外部工作区占用的等待默认30秒后保存部分进度返回。'),('比分共享缓存','缓存不可变元组，对外返回独立列表。'),('等待重复报价','非连续相同事件不重复占容量，冲突内容拒绝。'),('质量scope','合法但不适用的scope计数导出。'),('默认配置模板','与实际DEFAULT同步并添加一致性测试。'),('Linux环境','安装前检查64位Python3.11至3.13，复用兼容虚拟环境。')]
extra=[('扫描缓存、调度抢占、过期令牌、过期批次、进度和补充方向','DUPLICATE_FIXED','已归入第一份报告修复与回归。'),('版本、历史清单、过期验收数字','DOCUMENTED','当前验收等最终冻结回归后更新；历史证据不重写；根目录过期manifest重复副本已清除，原件保留历史目录。'),('非standard口径','SCOPE_CLARIFIED','standard为目标规格，旧档较弱口径明确披露。'),('代码指纹覆盖','SCOPE_CLARIFIED','engine_hash绑定计算源码、入口及运行依赖；release_hash覆盖前端、配置、测试和安装文件，两种用途不能混同。'),('交付目录链接和历史绝对路径','SCOPE_CLARIFIED','日常单入口不变；干净源码快照可独立放置；审计输入迁移不依赖旧路径。旧任务断点不可跨代码恢复。'),('全池两两对照','RECOMMENDATION_REJECTED','按用户全部候选要求保留完整范围；不擅自丢弃零重叠对或设置隐藏上限；成本受显式预算约束。'),('内容哈希和无manifest回退','RECOMMENDATION_REJECTED','不能用mtime或可变对象身份代替内容证据；正常流水线已有冻结预处理manifest。'),('前端与CLI预算','SCOPE_CLARIFIED','UI默认有限8192节点/方向、120分钟；CLI的0明确表示不限。有限预算结果不可标成全空间完成。'),('真实联赛、滚动、E01至E04、真实资金','NOT_RUN_OR_OUT_OF_SCOPE','用户要求暂停试跑；不伪造实测证据，也不擅自扩展真实资金或第二笔执行。'),('历史开发进度','DOCUMENTED','文件开头已明确历史记录和当前状态入口。')]
report={'source':str(source),'source_sha256':sha(source),'state':'AWAITING_FROZEN_REGRESSION','policy':'NO_RESEARCH_TRIALS','items':[{'id':f'ADD-{i+1:02d}','title':title,'status':'IMPLEMENTED_PENDING_FINAL_REGRESSION','resolution':note} for i,(title,note) in enumerate(fixed)]}
report['items'] += [{'id':f'ADD-{len(fixed)+i+1:02d}','title':title,'status':status,'resolution':note} for i,(title,status,note) in enumerate(extra)]
atomic_json(root/'validation/additional_review_resolution.json',report)
