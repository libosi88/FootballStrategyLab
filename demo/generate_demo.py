"""Clearly synthetic fixtures, NEVER evidence of real strategy profitability."""
from pathlib import Path
from datetime import datetime,timedelta
import csv
ROOT=Path(__file__).resolve().parent
qfields='日期,sId,联赛,主队,客队,开球时间,全场比分,半场比分,公司,盘口类型,阶段,比赛分钟,当时比分,盘口数值,盘口中文,上水/大球,下水/小球,变化时间,状态,封盘'.split(',')
ifields='日期,sId,联赛,主队,客队,开球时间,状态,全场比分,半场比分,让球val,大小val'.split(',')
for year in (2024,2025,2026):
 qq=[];ii=[]
 for i in range(45):
  t=datetime(year,3,1,19,30)+timedelta(days=i*3);sid=f'DEMO-{year}-{i:03d}';ft=(3,1) if i%5 else (0,1)
  common=dict(zip(ifields,[t.strftime('%Y-%m-%d'),sid,'合成演示联赛（非真实比赛）','合成主队','合成客队',t.strftime('%Y-%m-%d %H:%M'),'完',f'{ft[0]}-{ft[1]}','0-1','0.5','2.5']))
  ii.append(common)
  for market in ('让球','大小球'):
   for j in range(12):
    live=j>=4;minute=(j-4)*10 if live else ''
    clock=t+timedelta(minutes=int(minute)) if live else t-timedelta(minutes=(4-j)*20)
    score='0-1' if live else '';line=(0.5 if j%3 else 0.25) if market=='让球' else (2.5 if j<9 else 3)
    q={**common,'公司':'皇冠','盘口类型':market,'阶段':'滚球' if live else '赛前','比赛分钟':minute,'当时比分':score,'盘口数值':line,'盘口中文':str(line),
       '上水/大球':f'{0.95+(j%3)*.05:.2f}','下水/小球':f'{0.85-(j%3)*.05:.2f}','变化时间':clock.strftime('%Y-%m-%d %H:%M'),'状态':'滚' if live else '早' if j<2 else '即','封盘':''}
    qq.append(q)
 for filename,fields,rows in [(f'完整指数_演示_{year}.csv',qfields,qq),(f'比赛索引_演示_{year}.csv',ifields,ii)]:
  with (ROOT/filename).open('w',encoding='utf-8-sig',newline='') as f:
   w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
print('已生成明确标注的合成演示数据')
