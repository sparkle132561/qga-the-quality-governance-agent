<script setup lang="ts">
import {onMounted,ref} from 'vue'
import {useRouter} from 'vue-router'
import {actor,api,error,reportError} from './api'
import IncidentFields from './IncidentFields.vue'
type Draft={id:string;source_id:string;title:string;raw_text:string;source_sha256:string;revision:number;status:string;proposal:Record<string,unknown>|null;engine_mode:string;last_error:string|null;event_id:string|null}
const demoText='模拟公告：召回演示品牌 K170 电热水壶，生产日期2025-01-01至2025-04-30，原因把手松脱风险。'
const sources=ref<{id:string;title:string}[]>([]),drafts=ref<Draft[]>([]),busy=ref(false)
const sourceId=ref('demo-platform-policy'),title=ref('模拟K170质量事件'),rawText=ref(demoText)
const router=useRouter(),merchants=ref<{id:string;name:string}[]>([])
const targetMerchant=ref('merchant-a'),analysisDate=ref('2025-07-01')
function seed(row:Draft){
  const initial={event_type:'unknown',brand:null,models:[] as string[],production_scope:'unknown',production_start:null,production_end:null,batches:[] as string[],serial_scope_original:null,defect_summary:null,field_evidence:{} as Record<string,{quote:string}>}
  if(row.proposal){for(const key of Object.keys(initial))if(key in row.proposal)Object.assign(initial,{[key]:row.proposal[key]});return initial}
  if(row.raw_text===demoText)return {...initial,event_type:'recall',brand:'演示品牌',models:['K170'],production_scope:'range',production_start:'2025-01-01',production_end:'2025-04-30',defect_summary:'把手松脱风险',field_evidence:{event_type:{quote:'召回'},brand:{quote:'演示品牌'},models:{quote:'演示品牌 K170 电热水壶'},production_start:{quote:'生产日期2025-01-01至2025-04-30'},production_end:{quote:'生产日期2025-01-01至2025-04-30'},defect_summary:{quote:'把手松脱风险'}}}
  return initial
}
async function load(){
  if(actor.value?.role==='merchant')return
  busy.value=true
  try{[sources.value,drafts.value,merchants.value]=await Promise.all([api<{id:string;title:string}[]>('/sources'),api<Draft[]>('/incidents/drafts'),api<{id:string;name:string}[]>('/merchants')])}
  catch(cause){reportError(cause,'公告读取失败')}finally{busy.value=false}
}
async function create(){
  busy.value=true;error.value=''
  try{await api('/incidents/drafts',{method:'POST',body:JSON.stringify({source_id:sourceId.value,title:title.value,raw_text:rawText.value})});await load()}
  catch(cause){reportError(cause,'公告导入失败')}finally{busy.value=false}
}
async function extract(row:Draft){
  busy.value=true;error.value=''
  try{await api(`/incidents/drafts/${row.id}/extract`,{method:'POST',body:JSON.stringify({expected_revision:row.revision})})}
  catch(cause){reportError(cause,'抽取未完成')}
  finally{await load();busy.value=false}
}
async function recover(row:Draft){
  busy.value=true
  try{await api(`/incidents/drafts/${row.id}/recover`,{method:'POST'});await load()}
  catch(cause){reportError(cause,'抽取恢复失败')}finally{busy.value=false}
}
onMounted(load)
async function createImpact(row:Draft){
  busy.value=true;error.value=''
  try{
    const result=await api<{id:string}>('/cases',{method:'POST',body:JSON.stringify({title:Array.from('质量事件影响：'+row.title).slice(0,200).join(''),workflow:'incident',merchant_id:targetMerchant.value,payload:{event_id:row.event_id,as_of:analysisDate.value}})})
    await router.push('/cases/'+result.id)
  }catch(cause){reportError(cause,'影响案件创建失败')}finally{busy.value=false}
}
</script>
<template><main class="page"><section class="page-heading"><div><p class="eyebrow">INCIDENT INTAKE</p><h1>质量事件导入</h1><p class="muted">保留公告来源，核对范围字段，再进入影响分析。</p></div><button class="secondary" :disabled="busy" @click="load">刷新公告</button></section><p v-if="actor?.role==='merchant'" class="notice">公告草稿管理只对平台身份开放。已确认事件可在规则与来源查看。</p><template v-else><div class="notice"><strong>来源与范围边界</strong><p>粘贴文本不会自动与来源网址核对，新记录均标为模拟。召回和抽检分开，未知日期/序列号保留待核验，确认字段不会执行下架或通知。</p></div><form class="panel search-form" @submit.prevent="create"><h2>导入公告原文</h2><label>公告来源<select v-model="sourceId" required><option v-for="source in sources" :key="source.id" :value="source.id">{{source.title}}</option></select></label><label>公告标题<input v-model="title" required maxlength="250"></label><label>公告原文<textarea v-model="rawText" required rows="5" maxlength="12000"></textarea></label><button class="primary" :disabled="busy">保存公告草稿</button></form><section v-for="row in drafts" :key="row.id" class="panel catalog-panel"><div class="panel-heading"><h2>{{row.title}}</h2><span class="badge">{{row.status}} · v{{row.revision}}</span></div><p class="source-location">{{row.source_id}} · {{row.source_sha256}}</p><details class="rule-card"><summary>查看保存的公告原文</summary><p class="evidence-text">{{row.raw_text}}</p></details><p v-if="row.last_error" class="muted">{{row.last_error}} · {{row.engine_mode}}</p><div v-if="row.status!=='confirmed'" class="action-bar"><button v-if="row.status!=='extracting'" class="secondary" :disabled="busy" @click="extract(row)">模型抽取范围（需Agnes Key）</button><button v-else class="secondary" :disabled="busy" @click="recover(row)">恢复旧进程抽取</button></div><IncidentFields v-if="actor?.role==='reviewer' && !['confirmed','extracting'].includes(row.status)" :key="row.id+':'+row.revision" :id="row.id" :revision="row.revision" :seed="seed(row)" @confirmed="load" /><div v-if="row.event_id" class="notice"><strong>范围字段已保存为模拟事件</strong><p>{{row.event_id}} · 后续影响分析仍需核对候选商品与证据。</p><p v-if="row.proposal?.scope_review_required">仍待核验：{{row.proposal.unresolved_fields}}</p><form v-if="actor?.role==='reviewer'" class="search-form" @submit.prevent="createImpact(row)"><label>目标商家<select v-model="targetMerchant" required><option v-for="merchant in merchants" :key="merchant.id" :value="merchant.id">{{merchant.name}}</option></select></label><label>分析规则适用日期<input v-model="analysisDate" type="date" required></label><button class="secondary" :disabled="busy">创建影响分析案件</button></form></div></section><p class="muted">仅显示最近50份公告草稿。</p></template></main></template>
