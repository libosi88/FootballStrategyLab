"""The outcome-independent grammar from the user's frozen v3 specification.

These are finite domains, not a claim to enumerate every Boolean function.
Feature domains are discovered from legal past-observable feature values only.
"""
from itertools import product
from math import comb
from .common import MISSING

SPEC_VERSION='FSL_STANDARD_V3_1'
WATER_WIDTHS_W=(5,10,15,20,30,50)
WATER_WIDTHS_C=(10,15,20,30,50)
LINE_WIDTHS_W=(1,2,3,4,6,8)
LINE_WIDTHS_C=(2,4,6,8)
TIME_WIDTHS_W=(2,3,5,10,15,20,30,45)
TIME_WIDTHS_C=(5,10,15,20,30,45)
SPANS=(None,5,10,15,20,30,45,60)
LINE_STEPS=(1,2,3,4)
WATER_STEPS_PCT=(1,5,10,15,20)
TRADITIONAL_WINDOWS=((0,16),(16,31),(31,46),(46,61),(61,76),(76,91),(0,46),(46,91),(0,91))

def time_windows(level='W'):
    step=1 if level=='W' else 5
    widths=TIME_WIDTHS_W if level=='W' else TIME_WIDTHS_C
    return tuple(sorted(set(TRADITIONAL_WINDOWS)|{(lo,lo+width) for width in widths for lo in range(0,92-width,step)}))

def scalar_atoms(feature,lo,hi,unit,level='W',kind='water'):
    """Integral input units; thresholds extend outward to the frozen grid."""
    if lo==MISSING or hi==MISSING or lo>hi:return
    step=max(1,unit//20) if kind=='water' and level=='C' else 1
    first=(int(lo)//step)*step;last=-((-int(hi))//step)*step
    for value in range(first,last+1,step):
        for op in ('eq','le','ge'):yield {'feature':feature,'op':op,'value':value}
    widths=tuple(max(1,unit*p//100) for p in (WATER_WIDTHS_W if level=='W' else WATER_WIDTHS_C)) if kind=='water' else LINE_WIDTHS_W if level=='W' else LINE_WIDTHS_C if kind=='line' else ()
    if kind not in ('water','line'):widths=()
    for width in widths:
        interval_step=max(1,unit//100) if kind=='water' and level=='W' else step
        interval_first=(int(lo)//interval_step)*interval_step;interval_last=-((-int(hi))//interval_step)*interval_step
        for value in range(interval_first,interval_last+1,interval_step):
            yield {'feature':feature,'op':'range','value':value,'upper':value+width}

def minute_atoms(level='W'):
    for lo,hi in time_windows(level):yield {'feature':'minute','op':'range','value':lo,'upper':hi}
    yield {'feature':'minute','op':'eq','value':-2}
    if level=='W':
        for value in range(91):
            for op in ('eq','le','ge'):yield {'feature':'minute','op':op,'value':value}

def path_atoms(scale,timed=False,composite=False):
    """All 4^k patterns, both gap policies and event/state trigger policies.

    T is the full Cartesian product of applicable line and water step grids;
    an irrelevant step parameter is not used to inflate an expression count.
    """
    alphabet=(1,2,3,4) if not composite else (1,2,3,4,13,14,23,24)
    for mode,k in product(('keep','reset'),(1,2,3)):
        for bits in product(alphabet,repeat=k):
            codes=[{b} if b<10 else {b//10,b%10} for b in bits]
            has_line=any(c&{1,2} for c in codes);has_water=any(c&{3,4} for c in codes)
            for pulse in (False,True):
                base={'feature':f'path{k}_{mode}','op':'eq','value':int(''.join(map(str,bits))) if not composite else 0,'pulse':pulse}
                if composite:base.update(event_model='composite',pattern=list(bits))
                if not timed:
                    yield base;continue
                line_steps=LINE_STEPS if has_line else (None,)
                water_steps=tuple(max(1,scale*p//100) for p in WATER_STEPS_PCT) if has_water else (None,)
                for span,line_step,water_step,sequence in product(SPANS,line_steps,water_steps,('contiguous','subsequence')):
                    a={**base,'sequence':sequence}
                    if span is not None:a['span']=span
                    if line_step is not None:a['min_line_step']=line_step
                    if water_step is not None:a['min_water_step']=water_step
                    yield a

def required_templates(scale,ah):
    for feature in ('water','otherwater'):
        yield {'feature':feature,'op':'ge','value':scale*105//100}
        for lo,hi in ((95,105),(70,85),(85,100),(100,120)):
            yield {'feature':feature,'op':'range','value':scale*lo//100,'upper':scale*hi//100}
    if ah:
        yield {'feature':'absline','op':'range','value':1,'upper':3}
        yield {'feature':'absline','op':'range','value':12,'upper':14}
        yield {'feature':'absline','op':'ge','value':14}
    else:
        yield {'feature':'line','op':'range','value':12,'upper':14}
        yield {'feature':'line','op':'ge','value':14}

MODULES={
 'M00':'16个方向无附加条件基线',
 'M01':'W全域单原子与零命中账',
 'M02':'C全部合法双条件',
 'M03':'C全部合法三条件',
 'M04':'T单条件及一至二个C非路径上下文',
 'M05':'W中非C细原子及一至二个C非路径上下文',
 'M06':'前一完整分钟跨市场联合及一至二个C非跨市场上下文',
 'M07':'可靠赛前结束后的摘要及一至二个C滚球上下文',
 'M08':'语义参考、枚举/优化一致性、超门槛入池与覆盖审计',
}

def make_standard_plan(atom_count,core,timed,fine,cross,pregame,path_ids,blocked=None,max_conditions=3,version=SPEC_VERSION,modules=None):
    core=sorted(set(core));path_ids=set(path_ids)
    context=[i for i in core if i not in path_ids]
    blocks=[{'id':'M00_BASE','module':'M00','kind':'base','size':1},
            {'id':'M01_W','module':'M01','kind':'unary','size':atom_count},
            {'id':'M02_C2','module':'M02','kind':'comb','pool':core,'k':2,'size':comb(len(core),2) if len(core)>1 else 0}]
    if max_conditions>=3:blocks.append({'id':'M03_C3','module':'M03','kind':'comb','pool':core,'k':3,'size':comb(len(core),3) if len(core)>2 else 0})
    for module,anchors,pool in (('M04',timed,context),('M05',fine,context),('M06',cross,core),('M07',pregame,core)):
        if module!='M05':blocks.append({'id':module+'_UNARY','module':module,'kind':'unary_ids','pool':sorted(set(anchors)),'size':len(set(anchors))})
        for k in (1,2):
            blocks.append({'id':f'{module}_CONTEXT{k}','module':module,'kind':'anchored','anchors':sorted(set(anchors)),'pool':pool,'k':k,'size':len(set(anchors))*(comb(len(pool),k) if len(pool)>=k else 0)})
    return {'version':version,'blocks':blocks,'total':sum(b['size'] for b in blocks),'blocked':blocked or {},'modules':modules or MODULES,
            'count_kind':'raw_standard_expressions_before_exact_normalization','max_conditions':max_conditions,'extensions_included':False}

# ---- computable default grammar ---------------------------------------------------------------------
# The full v3 grammar (above) holds 10^11-10^14 expressions per direction and never finishes on a real
# league. The compact grammar is pre-registered and outcome-independent: coarse fixed grids clipped to
# each feature's observed domain, at most two conditions, every atom registered in priority order.
COMPACT_SPEC_VERSION='FSL_STANDARD_COMPACT_V1'
COMPACT_MAX_CONDITIONS=2
COMPACT_MODULES={
 'M00':'16个方向无附加条件基线',
 'M01':'紧凑网格全部单条件（盘口水位、阶段内变动、比赛状态、赛前摘要、另一市场、近期路径）',
 'M02':'紧凑网格全部两两组合',
 'M03':'紧凑语法不含三条件（v3完整语法扩展）',
 'M04':'紧凑语法不含时限路径（v3完整语法扩展）',
 'M05':'紧凑语法不含细网格（v3完整语法扩展）',
 'M06':'另一市场条件已并入M01/M02紧凑网格',
 'M07':'赛前摘要条件已并入M01/M02紧凑网格',
 'M08':MODULES['M08'],
}
COMPACT_WATER_LEVELS=(70,75,80,85,90,95,100,105,110,115,120)
COMPACT_WATER_MOVES=(5,10,15)
COMPACT_LINE_MOVES=(1,2,4)
COMPACT_MINUTE_WINDOWS=((0,16),(16,31),(31,46),(46,61),(61,76),(76,91),(0,46),(46,91))

def grammar_of(config):return (config or {}).get('search_grammar','compact_v1')
def max_conditions_for(config):return COMPACT_MAX_CONDITIONS if grammar_of(config)=='compact_v1' else 3
def spec_version_for(config):return {'compact_v1':COMPACT_SPEC_VERSION,'balanced_v1':'FSL_STANDARD_BALANCED_V1','v3_full':SPEC_VERSION}[grammar_of(config)]
def modules_for(config):
    if grammar_of(config)=='balanced_v1':
        return {**COMPACT_MODULES,'M01':'中等规格完整单条件：紧凑核心、必要阈值、基准与1至3步路径',
                'M02':'中等核心全部两条件','M03':'中等核心全部三条件',
                'M04':'本规格不包含时限路径，属于v3_full',
                'M05':'补充基准/路径原子与一至二个核心非路径上下文'}
    return COMPACT_MODULES if grammar_of(config)=='compact_v1' else MODULES

def balanced_atoms(direction,scale,domain,values):
    """Fixed middle scope, never expanded from profitable seeds.

    True marks a core atom (all core pairs/triples); False is a supplement
    crossed with up to two non-path core contexts in M05. No timed paths.
    """
    for a in compact_atoms(direction,scale,domain,values):yield a,True
    ah=not direction.endswith(('OVER','UNDER'))
    for a in required_templates(scale,ah):yield a,True
    for feature in ('samewater','other_samewater','water_init','water_prev_keep'):
        for step in (5,10,15,20):
            for op,sign in (('le',-1),('ge',1)):
                yield {'feature':feature,'op':op,'value':sign*max(1,scale*step//100)},True
    for feature in ('returnwater_keep','returnwater_reset','water_prev_reset',
                    'water_from_min','water_from_max','other_water_init'):
        for step in (5,10,15,20):
            for op,sign in (('le',-1),('ge',1)):
                yield {'feature':feature,'op':op,'value':sign*max(1,scale*step//100)},False
    for step in (1,2,3,4):
        for op,sign in (('le',-1),('ge',1)):
            yield {'feature':'line_init','op':op,'value':sign*step},True
    for a in path_atoms(scale,timed=False,composite=False):yield a,False
    for mode in ('keep','reset'):
        for code in (11,12,21,22):yield {'feature':'linepath2_'+mode,'op':'eq','value':code},False
    if direction.startswith('LIVE'):
        for lo,hi in COMPACT_MINUTE_WINDOWS:
            for kind,steps in (('water_init',tuple(max(1,scale*p//100) for p in (5,10,15,20))),('line_init',(1,2,3,4))):
                for step in steps:
                    for op,sign in (('le',-1),('ge',1)):
                        yield {'feature':f'window_{lo}_{hi}_{kind}','op':op,'value':sign*step},False

def compact_atoms(direction,scale,domain,values):
    """Yield the compact grammar for one direction. domain(feature)->(min,max)|None and values(feature)->set|None
    describe legal past-observable feature values only; thresholds outside the observed domain are never generated."""
    live=direction.startswith('LIVE');ah=not direction.endswith(('OVER','UNDER'))
    water=lambda p:max(1,scale*p//100)
    levels_w=tuple(water(p) for p in COMPACT_WATER_LEVELS);moves_w=tuple(water(p) for p in COMPACT_WATER_MOVES)
    def levels(feature,grid):
        span=domain(feature)
        if span is None:return
        lo,hi=span
        yield from ({'feature':feature,'op':'le','value':v} for v in grid if lo<=v<hi)
        yield from ({'feature':feature,'op':'ge','value':v} for v in grid if lo<v<=hi)
    def moves(feature,steps):
        span=domain(feature)
        if span is None or span[0]==span[1]:return
        lo,hi=span
        if lo<=0<=hi:yield {'feature':feature,'op':'eq','value':0}
        for step in steps:
            if lo<=-step<hi:yield {'feature':feature,'op':'le','value':-step}
            if lo<step<=hi:yield {'feature':feature,'op':'ge','value':step}
    def tails(feature,low=None,high=None):
        span=domain(feature)
        if span is None:return
        lo,hi=span
        if low is not None and lo<=low<hi:yield {'feature':feature,'op':'le','value':low}
        if high is not None and lo<high<=hi:yield {'feature':feature,'op':'ge','value':high}
    def codes(feature,grid):
        seen=values(feature)
        if not seen or len(seen)<2:return
        yield from ({'feature':feature,'op':'eq','value':v} for v in grid if v in seen)
    # 1. price level
    yield from levels('absline',range(0,11)) if ah else levels('line',range(2,21))
    yield from levels('water',levels_w);yield from levels('otherwater',levels_w)
    # 2. movement within the phase
    yield from moves('line_init',COMPACT_LINE_MOVES)
    yield from moves('water_init',moves_w);yield from moves('other_water_init',moves_w)
    yield from moves('samewater',moves_w[:2]);yield from moves('water_prev_any_keep',moves_w[:1]);yield from moves('line_prev_keep',COMPACT_LINE_MOVES[:1])
    if ah:yield from moves('role_water_init',moves_w[:2])
    if live:
        # 3. match state
        for lo,hi in COMPACT_MINUTE_WINDOWS:yield {'feature':'minute','op':'range','value':lo,'upper':hi}
        yield {'feature':'minute','op':'eq','value':-2}
        yield from codes('goal_diff',(-1,0,1));yield from tails('goal_diff',-2,2)
        yield from codes('total_goals',(0,1,2));yield from tails('total_goals',high=3)
        if ah:yield from codes('role_diff',(-1,0,1));yield from tails('role_diff',-2,2)
        for lo,hi in ((0,46),(46,91)):
            yield from moves(f'window_{lo}_{hi}_line_init',COMPACT_LINE_MOVES[:1]);yield from moves(f'window_{lo}_{hi}_water_init',moves_w[1:2])
        # 4. finished prematch summary
        yield from levels('pre_line_close',range(-10,11,2) if ah else range(4,17,2))
        yield from moves('pre_line_change',COMPACT_LINE_MOVES[:2]);yield from moves('pre_water_change',moves_w[:2])
    # 5. other market at the previous complete minute
    yield from levels('cross_line',range(6,17,2) if ah else range(-10,11,2))
    yield from moves('cross_line_init',COMPACT_LINE_MOVES[:1])
    # 6. recent quote path (line-first projection, persistent state, kept across closures)
    yield from codes('path1_keep',(1,2,3,4))
    yield from codes('path2_keep',tuple(a*10+b for a in range(1,5) for b in range(1,5)))
    yield from codes('linepath2_keep',(11,12,21,22))

def required_difference_templates(feature,scale,kind):
    """Mandatory signed moves remain registered even outside observed domains."""
    if kind not in ('line','water') or not any(token in feature for token in ('init','prev','from_','return','same','change')) or feature.endswith('_changes'):return
    for step in ((1,2,3,4) if kind=='line' else tuple(scale*p//100 for p in (5,10,15,20))):
        yield {'feature':feature,'op':'ge','value':step}
        yield {'feature':feature,'op':'le','value':-step}


def capability_state(implemented,tests=None):
    tests=tests or {}
    return {'spec_version':SPEC_VERSION,'modules':{m:{'description':d,'implementation':'IMPLEMENTED' if m in implemented else 'NOT_IMPLEMENTED','verification':tests.get(m,'NOT_RUN')} for m,d in MODULES.items()},
            'standard_complete':all(m in implemented and tests.get(m)=='PASS' for m in MODULES)}
