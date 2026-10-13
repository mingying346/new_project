const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const fmt=(x,d=2)=>x==null?'—':(typeof x==='number'?x.toLocaleString('zh-CN',{maximumFractionDigits:d}):x);
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error((await r.text()).slice(0,200));return r.json()}

$$('nav button').forEach(b=>b.onclick=()=>{$$('nav button').forEach(x=>x.classList.remove('on'));
  $$('main section').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  $('#'+b.dataset.t).classList.add('on');
  if(b.dataset.t==='t4')$('#rep').src='/report?t='+Date.now();
  if(b.dataset.t==='t6'){loadImport();refreshJob(true);}});

const PARAMS={weights:{},thresholds:{}};
async function boot(){
  try{
    const s=await j('/api/summary');
    $('#meta').textContent=`流水 ${fmt(s.流水.n,0)} 笔 · ${s.流水.a} ~ ${s.流水.b} · 话单 ${fmt(s.话单.n,0)} · 寄递 ${fmt(s.寄递.n,0)} · 分析主体 ${fmt(s.主体.n,0)} · 库 ${s.size_mb} MB`;
    const K=[['资金流水',fmt(s.流水.n,0),s.流水.a+' ~ '+s.流水.b],
             ['话单记录',fmt(s.话单.n,0),''],
             ['寄递记录',fmt(s.寄递.n,0),''],
             ['台账人员',fmt(s.台账.n,0),'去重身份证 '+fmt(s.台账.id,0)],
             ['分析主体',fmt(s.主体.n,0),''],
             ['质量条目',(s.质量条目||[]).reduce((a,b)=>a+b.n,0).toLocaleString(),'']];
    $('#kpis').innerHTML=K.map(k=>`<div class="kpi"><span>${k[0]}</span><b>${k[1]}</b>
      <span class="dim" style="font-size:11px">${k[2]||''}</span></div>`).join('');
    loadQuality();loadConv();loadSubjects();loadGraph();
    $('#rep').src='/report?t='+Date.now();
  }catch(e){
    const noDb=/未找到分析库/.test(e.message);
    $('#meta').textContent = noDb
      ? '尚未产生分析结果 —— 请到「⑥ 导入与处理」导入数据并点"开始处理"'
      : '初始化失败：'+e.message;
    if(noDb) $('#kpis').innerHTML='<div class="kpi"><span>当前状态</span><b>未使用</b>'
      +'<span class="dim" style="font-size:11px">尚无分析库；导入数据并开始处理后自动生成</span></div>';
  }
}
function tbl(el,rows,cols,onclick){
  if(!rows.length){el.innerHTML='<tr><td class="dim">无数据</td></tr>';return}
  const head='<tr>'+cols.map(c=>`<th>${c[0]}</th>`).join('')+'</tr>';
  const body=rows.map((r,i)=>`<tr ${onclick?`onclick="${onclick}(${i})" style="cursor:pointer"`:''}>`+
    cols.map(c=>`<td>${cell(r,c[1],c[2])}</td>`).join('')+'</tr>').join('');
  el.innerHTML=head+body; el._rows=rows; el._cols=cols;
}
function cell(v,k,f){
  let x=v[k]; if(x==null)return '<span class="dim">—</span>';
  if(f==='num')return fmt(x, f==='num'?2:2);
  if(f==='money')return fmt(Number(x)/100,2);
  if(f==='pct')return x==null?'—':(Number(x)*100).toFixed(1)+'%';
  if(f==='pill')return `<span class="pill ${x}">${x}</span>`;
  if(f==='mono')return `<span class="mono">${String(x).slice(0,120)}</span>`;
  if(f==='bar')return `<div class="bar"><i style="width:${Math.min(100,x)}%"></i></div>${fmt(x,0)}`;
  return String(x).replace(/</g,'&lt;').slice(0,160);
}
async function loadQuality(){
  const r=await j(`/api/quality?category=${encodeURIComponent($('#qcat').value)}&q=${encodeURIComponent($('#qq').value)}&limit=300`);
  tbl($('#qt'),r,[['类别','category'],['严重度','severity'],['数据集','dataset'],['来源文件','path','mono'],
    ['原始行','src_line'],['字段','field'],['说明','detail'],['处理动作','action']]);
}
async function loadConv(){
  const r=await j('/api/convlog?limit=1200');
  tbl($('#ct'),r,[['数据集','数据集'],['文件','文件','mono'],['编码','编码'],['表头列数','表头列数'],
    ['记录数','记录数'],['重复行','重复行'],['列数异常','字段数异常'],['不可见字符/重名','不可见字符'],
    ['关键空值','缺失单元格'],['重复来源文件','重复来源文件'],['时间跨度','时间跨度']]);
}
async function loadSubjects(){
  const p=new URLSearchParams({limit:300,band:$('#fband').value,role:$('#frole').value,q:$('#fq').value});
  const r=await j('/api/subjects?'+p);
  if(!$('#frole').options.length){
    [...new Set(r.map(x=>x.role))].forEach(x=>{const o=document.createElement('option');o.textContent=x;$('#frole').append(o)});
  }
  tbl($('#st'),r,[['分档','band','pill'],['对象','label'],['角色','role'],['综合分','total','bar'],
    ['流水笔数','n'],['收款(元)','in_amt'],['付款(元)','out_amt'],['收款对手','in_peer_n'],
    ['提现率','withdraw_rate','pct'],['3日转付比','turn3d_rate','pct'],['夜间占比','night_share','pct'],
    ['定频对手','recurring_peer_n'],['年龄','age'],['身份','resolved'],['身份证','id_card','mono'],['数据源','sources']],
    'openSubject');
  window._subj=r;
}
function openSubject(i){
  const k=window._subj[i].key;
  j('/api/subject/'+encodeURIComponent(k)).then(d=>{
    const f=d.feat, s=d.score||{};
    const P=$('#panel');P.style.display='block';
    P.innerHTML=`<h3>${f.label||k} <span class="pill ${s.band||'低'}">${s.band||''}</span></h3>
    <div class="dim mono">${k}</div>
    <div class="kv"><span>角色</span><span>${s.role||''}（综合分 ${fmt(s.total,1)}）</span></div>
    <div class="kv"><span>身份证(掩码)</span><span>${f.id_card||'待核查'}</span></div>
    <div class="kv"><span>关联微信账号</span><span>${(f.accounts||'').slice(0,60)}</span></div>
    <div class="kv"><span>收 / 付</span><span>${fmt(f.in_amt)} / ${fmt(f.out_amt)} 元</span></div>
    <div class="kv"><span>净流入</span><span>${fmt(f.net_amt)} 元</span></div>
    <div class="kv"><span>收款对手 / 付款对手</span><span>${f.in_peer_n} / ${f.out_peer_n}</span></div>
    <div class="kv"><span>提现率 / 3日转付比 / 夜间占比</span><span>${fmt(f.withdraw_rate,3)} / ${fmt(f.turn3d_rate,3)} / ${fmt(f.night_share,3)}</span></div>
    <div class="kv"><span>定频收款对手 / 双向往来</span><span>${f.recurring_peer_n} / ${f.duplex_peer_n}</span></div>
    <div class="kv"><span>寄递 发/收/疑似药品</span><span>${f.parcel_send_n} / ${f.parcel_recv_n} / ${f.drug_parcel_n}</span></div>
    <div class="kv"><span>通话次数 / 三源共现天数</span><span>${f.call_n} / ${f.multi_src_days}</span></div>
    <div class="kv"><span>问题数据行</span><span>${f.dirty_rows}</span></div>
    <h4>触发指标</h4>${(d.components&&d.components.length?d.components:[]).map(t=>
      `<div>• <b>${t[0]}</b>=${fmt(t[1],3)} — ${t[2]}</div>`).join('')||'<span class="dim">无</span>'}
    <h4>其它可行解释（反证）</h4>${((s.alt&&JSON.parse(s.alt))||[]).map(a=>`<div>• ${a}</div>`).join('')||'<span class="dim">无</span>'}
    <h4>大额交易原始定位（点行可回溯）</h4>
    <table><tr><th>时间</th><th>收/付</th><th>金额</th><th>对手</th><th>来源:行号</th></tr>
    ${(d.evidence||[]).map(e=>`<tr><td>${e.ts}</td><td>${e.dc==='入'?'收':'付'}</td>
      <td>${fmt(e.amt)}</td><td>${(e.peer_name||e.peer_id||'').slice(0,16)}</td>
      <td class="mono">${String(e.path||'').replace(/\\/g,'/').split('/').pop()}:${e.src_line}</td></tr>`).join('')}</table>
    <h4>关联边</h4>${(d.edges||[]).slice(0,10).map(e=>`<div class="dim">
      ${e.src==='X'?'':''}${e.src} → ${e.dst} · ${e.kind} · ${e.n} 次 · ${fmt(e.amount_fen/100)} 元</div>`).join('')}
    <div style="margin-top:10px"><button onclick="document.getElementById('panel').style.display='none'">关闭</button></div>`;
  }).catch(e=>alert('加载失败 '+e.message));
}
async function renderParams(){
  const s=await j('/api/summary');
  const th={withdraw_rate_hi:0.6,turn3d_hi:0.8,in_peer_n_hi:8,night_hi:0.25,p2p_hi:0.6,
    merchant_lo:0.25,round_hi:0.25,recurring_n:3,min_in_amt:20000,band_hi:65,band_mid:45};
  const names={withdraw_rate_hi:'提现率阈值',turn3d_hi:'3日转付比阈值',in_peer_n_hi:'收款对手数阈值',
    night_hi:'夜间占比阈值',p2p_hi:'转账占比阈值',merchant_lo:'商户占比下限',round_hi:'整额占比阈值',
    recurring_n:'定频对手数阈值',min_in_amt:'收款额下限(元)',band_hi:'高档分数线',band_mid:'中档分数线'};
  $('#prm').innerHTML=Object.keys(names).map(k=>
    `<label>${names[k]} <input data-th="${k}" value="${th[k]}" style="width:90px"></label>`).join('')+
    `<label>权重·归集强度 <input data-w="归集强度" value="0.20" style="width:70px"></label>
     <label>权重·收购广度 <input data-w="收购广度" value="0.18" style="width:70px"></label>
     <label>权重·时段异常 <input data-w="时段异常" value="0.10" style="width:70px"></label>
     <label>权重·非消费性 <input data-w="非消费性" value="0.14" style="width:70px"></label>
     <label>权重·定频结算 <input data-w="定频结算" value="0.12" style="width:70px"></label>
     <label>权重·网络位置 <input data-w="网络位置" value="0.14" style:70px" style="width:70px"></label>
     <label>权重·跨源印证 <input data-w="跨源印证" value="0.12" style="width:70px"></label>`;
}
async function rescreen(){
  const thresholds={},weights={};
  $$('[data-th]').forEach(i=>thresholds[i.dataset.th]=parseFloat(i.value));
  $$('[data-w]').forEach(i=>weights[i.dataset.w]=parseFloat(i.value));
  $('#meta').textContent='重算中…';
  try{
    const r=await j('/api/rescreen',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({thresholds,weights})});
    $('#meta').textContent=`重算完成（${r.seconds}s，${r.count} 个主体）`;
    loadSubjects();
  }catch(e){alert('重算失败：'+e.message);$('#meta').textContent='重算失败'}
}
const COL={"上游-疑似医保开药人":"#3fb950","1级收药人":"#f0883e","2级收药人":"#d29922",
  "资金归集端":"#f85149","下游-诊所药房":"#58a6ff","自用/末端":"#8b949e","待核查":"#a371f7"};
function loadGraph(){
  // 默认直接读取已导出的静态图谱成品（0.1s，且与交付物完全一致）；
  // 只有勾选"实时重建"才调用 /graph 动态构图（每次约 15s，会重跑 analyse()）。
  const live=$('#glive').checked;
  const p=new URLSearchParams({mode:$('#gmode').value});
  if(live){ p.set('top',$('#gtop').value||40); p.set('min_amount',$('#gmin').value||0); }
  else p.set('static','1');
  $('#gframe').src='/graph?'+p.toString()+(live?'&t='+Date.now():'');
  $('#gnote').innerHTML=live
    ? '<span style="color:#d29922">正在按参数实时重建图谱（约15秒）…</span> 方向 = 付款方 → 收款方；虚线节点 = 身份待核查。'
    : '已读取静态成品 <code>output/03_关系图谱/资金关系图谱'+($('#gmode').value==='full'?'':'_核心网')+'.html</code>（与交付物一致，秒开）。'
      +'改参数请勾选"实时重建"，或先跑 <code>python app/pipeline.py graph</code> 重新导出。';
}
async function regen(){
  $('#meta').textContent='报告生成中…';
  const r=await j('/api/rerun_report',{method:'POST'});
  $('#meta').textContent='报告已重新生成';$('#rep').src='/report?t='+Date.now();
}
async function ask(){
  const q=$('#qi').value.trim();if(!q)return;
  const box=$('#chatlog');
  box.insertAdjacentHTML('beforeend',`<div class="msg"><b>问：</b>${q}</div>`);
  $('#meta').textContent='研判智能体查询中…';
  let d;
  try{ d=await j('/api/chat?q='+encodeURIComponent(q)+'&engine='+encodeURIComponent($('#eng').value)
        +'&history='+encodeURIComponent(JSON.stringify(_hist.slice(-4)))); }
  catch(e){ $('#meta').textContent='查询失败';
    box.insertAdjacentHTML('beforeend',`<div class="msg" style="border-color:#f85149">
      请求失败：${e.message}</div>`); return; }
  _hist.push({role:'user',content:q},{role:'assistant',content:(d.reply||'').slice(0,1500)});
  const tl=(d.tools||[]).map(x=>`${x.tool}(${x.rows}条)`).join(' → ');
  box.insertAdjacentHTML('beforeend',`<div class="msg" style="border-left-color:${d.engine&&d.engine.startsWith('rules')?'#8b949e':'#3fb950'}">
   <div class="dim" style="font-size:11px;margin-bottom:4px">引擎 ${d.engine||''}
    ${tl?'｜工具链 '+tl:''}${d.degraded?'｜⚠ '+d.degraded:''}</div>`);
  $('#meta').textContent=`对话完成（引擎 ${d.engine}，工具 ${(d.tools||[]).length} 次）`;
  let h=`<div class="msg"><b>答：</b>${d.reply||''}`;
  if(d.top)h+='<table><tr><th>排名</th><th>名称</th><th>角色</th><th>综合分</th><th>分档</th></tr>'+
    d.top.map(t=>`<tr><td>${t['排名']}</td><td>${t['名称']}</td><td>${t['角色']}</td><td>${fmt(t['综合分'],1)}</td>
    <td><span class="pill ${t['分档']}">${t['分档']}</span></td></tr>`).join('')+'</table>';
  if(d.rows&&d.rows.length){h+=miniTable(d.rows);}
  if(d.components)h+='<h4>触发指标</h4>'+d.components.map(t=>`<div>• <b>${t[0]}</b>=${fmt(t[1],3)} — ${t[2]}</div>`).join('');
  if(d.alternatives&&d.alternatives.length)h+='<h4>其它可行解释</h4>'+d.alternatives.map(a=>`<div>• ${a}</div>`).join('');
  if(d.evidence&&d.evidence.length)h+='<h4>原始交易定位</h4>'+miniTable(d.evidence);
  h+='</div>';box.insertAdjacentHTML('beforeend',h);box.scrollTop=1e9;
}
const _hist=[];
function miniTable(rows){
  const ks=Object.keys(rows[0]).filter(k=>typeof rows[0][k]!=='object');
  return `<div class="wrap" style="max-height:280px;margin-top:6px"><table><tr>`+
    ks.map(k=>`<th>${k}</th>`).join('')+`</tr>`+rows.slice(0,40).map(r=>`<tr>`+
      ks.map(k=>`<td>${String(r[k]==null?'':r[k]).slice(0,60)}</td>`).join('')+`</tr>`).join('')+`</table></div>`;
}
/* ============================ ⑥ 导入与处理 ============================ */
function pick(id){document.getElementById(id).click()}
async function picked(inp,isDir){const fs=[...inp.files];inp.value='';if(fs.length)await uploadAll(fs,isDir)}
async function uploadAll(fs,isDir){
  const st=$('#fstatus');let ok=0,fail=0;
  for(let i=0;i<fs.length;i++){
    const f=fs[i], rel=(isDir&&f.webkitRelativePath)?f.webkitRelativePath:f.name;
    st.textContent=`上传中 ${i+1}/${fs.length}：${rel}`;
    try{
      const fd=new FormData();fd.append('file',f);
      const r=await fetch('/api/import/upload?rel='+encodeURIComponent(rel),{method:'POST',body:fd});
      if(!r.ok)throw new Error((await r.text()).slice(0,140));
      ok++;
    }catch(e){fail++;st.textContent=`第 ${i+1} 个文件上传失败（${rel}）：${e.message}`;
      await new Promise(z=>setTimeout(z,1200));}
  }
  st.textContent=`上传完成：成功 ${ok} 个${fail?`，失败 ${fail} 个`:''}`;
  loadImport();
}
async function loadImport(){
  try{
    const d=await j('/api/import/files');
    window._impCount=d.count;
    $('#fstatus').textContent=d.count
      ? `已导入 ${d.count} 个文件，合计 ${d.total_mb} MB`:`尚未导入任何文件`;
    tbl($('#ft'),d.files.map(x=>({rel:x.rel,mb:(x.bytes/1048576).toFixed(2)})),
        [['相对路径','rel','mono'],['大小(MB)','mb']]);
  }catch(e){$('#fstatus').textContent='读取导入目录失败：'+e.message}
}
async function clearImport(){
  if(!(window._impCount>0)){alert('导入目录本来就是空的');return}
  if(!confirm(`确认删除导入目录中全部 ${window._impCount} 个文件？`))return;
  try{await j('/api/import/clear',{method:'POST'});loadImport();}catch(e){alert('清空失败：'+e.message)}
}
const dz=$('#drop');
['dragover','dragenter'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add('over')}));
['dragleave','drop'].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.remove('over')}));
dz.addEventListener('drop',e=>{const fs=[...e.dataTransfer.files];if(fs.length)uploadAll(fs,0)});

let _jobTimer=null,_jobRunning=false,_resFetched=false;
async function startJob(){
  const mode=$('#pmode').value, excel=$('#pexcel').checked;
  if(mode==='import'&&!(window._impCount>0)){alert('"仅分析导入数据"模式需要先导入文件');return}
  const msg=mode==='merge'
    ? '将对「原始取证 + 导入数据」重建全部分析结果（覆盖 output/ 对应产物），确定开始？'
    : '将只用「导入数据」重建分析结果，原始取证不参与（覆盖 output/ 对应产物），确定开始？';
  if(!confirm(msg))return;
  try{
    await j('/api/pipeline/start',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({mode,excel})});
    _resFetched=false;$('#jnote').textContent='处理已启动…';
    $('#rep').src='about:blank';$('#gframe').src='about:blank';
    refreshJob(true);ensureJobPoll();
  }catch(e){alert('启动失败：'+e.message)}
}
async function stopJob(){
  if(!confirm('停止当前处理任务？已完成阶段的结果保留，未执行阶段不会运行。'))return;
  try{await j('/api/pipeline/stop',{method:'POST'});refreshJob(true);}catch(e){alert('停止失败：'+e.message)}
}
function ensureJobPoll(){if(!_jobTimer)_jobTimer=setInterval(()=>refreshJob(),1500)}
async function refreshJob(force){
  const visible=$('#t6').classList.contains('on');
  if(!visible&&!_jobRunning&&!force){if(_jobTimer){clearInterval(_jobTimer);_jobTimer=null}return}
  let s;try{s=await j('/api/pipeline/status')}catch(e){return}
  await renderJob(s);
  _jobRunning=s.running;
  if(!s.running&&!visible&&_jobTimer){clearInterval(_jobTimer);_jobTimer=null}
}
async function renderJob(s){
  const st=s.stages||[];
  const doneN=st.filter(x=>x.status==='done').length;
  $('#jbar').firstElementChild.style.width=(st.length?Math.round(doneN/st.length*100):0)+'%';
  const icon={pending:'○',running:'◐',done:'●',failed:'✕',skipped:'－'};
  $('#jstages').innerHTML=st.map(x=>{
    const cls={running:'run',done:'done',failed:'fail',skipped:'skip'}[x.status]||'';
    return `<span class="stg ${cls}">${icon[x.status]||'○'} ${x.name}`+
           `${x.seconds!=null?`（${x.seconds}s）`:''}</span>`}).join('');
  const lg=$('#jlog');lg.textContent=(s.log_tail||[]).join('\n');
  lg.parentElement.scrollTop=lg.parentElement.scrollHeight;
  $('#jstart').disabled=s.running;$('#jstop').disabled=!s.running;
  if(s.running){
    $('#jnote').innerHTML=`<span style="color:#d29922">处理中…</span> 已用时 ${fmt(s.elapsed,0)} 秒`;
  }else if(s.started&&s.rc===0){
    let html=`<span style="color:#3fb950">处理完成</span>（${s.started} → ${s.finished}）
      <button class="act" onclick="location.reload()">刷新查看结果</button>`;
    if(!_resFetched){_resFetched=true;
      try{const m=await j('/api/summary');
        html+=` <span class="dim">流水 ${fmt(m.流水.n,0)} · 话单 ${fmt(m.话单.n,0)} · 寄递 ${fmt(m.寄递.n,0)}`
             +` · 分析主体 ${fmt(m.主体.n,0)} · 质量条目 ${(m.质量条目||[]).reduce((a,b)=>a+b.n,0).toLocaleString()}</span>`;
      }catch(e){}}
    $('#jnote').innerHTML=html;
  }else if(s.started){
    $('#jnote').innerHTML=`<span style="color:#f85149">处理未完成：${s.error||'未知错误'}</span>`;
  }else{$('#jnote').textContent='尚未运行处理任务';}
}
/* ====================================================================== */
renderParams();boot();loadImport();
