<script setup lang="ts">
import {onMounted,onUnmounted,ref,watch} from 'vue'
import {actor,api,error,reportError} from './api'
import PathPanel from './PathPanel.vue'
const props=defineProps<{caseId?:string}>()
type Node={id:string;kind:string;label:string;origin:string;is_demo:boolean}
type Edge={subject:string;predicate:string;object:string;origin:string;quote?:string;provenance:Record<string,unknown>;is_demo:boolean}
type Graph={nodes:Node[];edges:Edge[];elapsed_ms:number;notice:string}
type Extraction={id:string;chunk_id:string;status:string;is_current:boolean;engine_mode:string;review_note:string|null;body:{entities:{key:string;kind:string;name:string;qualifiers:Record<string,string>}[];relations:{subject:string;predicate:string;object:string;quote:string}[]};location:Record<string,unknown>}
const asOf=ref('2025-07-01'),chunkId=ref(''),note=ref(''),busy=ref(false)
const graph=ref<Graph|null>(null),extractions=ref<Extraction[]>([])
let loadRevision=0
watch([asOf,()=>props.caseId],()=>{loadRevision++;graph.value=null;extractions.value=[];busy.value=false})
onUnmounted(()=>{loadRevision++})
function scope(){return `as_of=${asOf.value}${props.caseId?'&case_id='+encodeURIComponent(props.caseId):''}`}
async function load(){
  busy.value=true
  const revision=++loadRevision
  try{
    const results=await Promise.all([api<Graph>(`/graph?${scope()}`),api<Extraction[]>(`/graph/extractions?${scope()}`)])
    if(revision===loadRevision)[graph.value,extractions.value]=results
  }catch(cause){if(revision===loadRevision)reportError(cause,'图证据读取失败')}
  finally{if(revision===loadRevision)busy.value=false}
}
async function extract(){
  busy.value=true;error.value=''
  try{
    await api('/graph/extractions',{method:'POST',body:JSON.stringify({chunk_id:chunkId.value.trim(),as_of:asOf.value,case_id:props.caseId})})
    await load()
  }catch(cause){reportError(cause,'文档关系抽取失败')}finally{busy.value=false}
}
async function review(id:string,decision:string){
  if(!note.value.trim()){error.value='请填写核验依据，再提交关系决定。';return}
  busy.value=true;error.value=''
  try{
    await api(`/graph/extractions/${id}/review`,{method:'POST',body:JSON.stringify({decision,note:note.value,as_of:asOf.value})})
    await load()
  }catch(cause){reportError(cause,'关系核验失败')}finally{busy.value=false}
}
onMounted(load)
</script>
<template><section class="panel catalog-panel"><div class="panel-heading"><h2>业务与文档关系</h2><form class="date-query" @submit.prevent="load"><label>图适用日期<input v-model="asOf" type="date" required></label><button class="secondary" :disabled="busy">刷新关系</button></form></div><p class="muted">业务关系来自当前身份可见的目录记录。文档关系需逐条核验；同名实体保留来源，不自动合并。</p><p v-if="graph" class="source-location">{{graph.nodes.length}} 个实体 · {{graph.edges.length}} 条关系 · {{graph.elapsed_ms.toFixed(1)}} ms</p><details v-if="graph?.edges.length"><summary>查看已纳入的关系</summary><div class="table-wrap"><table><thead><tr><th>起点</th><th>关系</th><th>终点</th><th>来源</th></tr></thead><tbody><tr v-for="(edge,index) in graph.edges" :key="index"><td>{{edge.subject}}</td><td>{{edge.predicate}}</td><td>{{edge.object}}</td><td><small>{{edge.origin}} · {{edge.is_demo?'模拟':'来源参考'}}</small><details><summary>原始依据</summary><pre class="event-json">{{JSON.stringify(edge.provenance,null,2)}}</pre><p v-if="edge.quote">{{edge.quote}}</p></details></td></tr></tbody></table></div></details><p v-else class="muted">当前没有可用关系。管理员可先导入演示目录。</p><form class="search-form" @submit.prevent="extract"><label>证据块 ID<input v-model="chunkId" required maxlength="36" placeholder="从检索结果复制来源块 ID"></label><button class="secondary" :disabled="busy">抽取文档关系（需Agnes Key）</button></form><label v-if="actor?.role!=='merchant'">关系核验依据<textarea v-model="note" rows="2" maxlength="500" placeholder="核对原文和身份限定；确认或拒绝的原因"></textarea></label><details v-for="row in extractions" :key="row.id" class="rule-card"><summary>{{row.status}} · {{row.is_current?'当前来源':'已失效'}} · {{row.engine_mode}}</summary><small class="source-location">{{row.chunk_id}} · {{JSON.stringify(row.location)}}</small><ul><li v-for="entity in row.body.entities" :key="entity.key">{{entity.key}} · {{entity.kind}} · {{entity.name}} · {{JSON.stringify(entity.qualifiers)}}</li></ul><p v-for="(relation,index) in row.body.relations" :key="index">{{relation.subject}} → {{relation.predicate}} → {{relation.object}}<br>原文依据：{{relation.quote}}</p><p v-if="row.review_note">核验：{{row.review_note}}</p><div v-if="actor?.role!=='merchant' && row.status==='pending' && row.is_current" class="action-bar"><button class="secondary" :disabled="busy" @click="review(row.id,'confirmed')">确认文档关系</button><button class="secondary" :disabled="busy" @click="review(row.id,'rejected')">拒绝文档关系</button></div></details><p class="muted">显示最近100条抽取记录。类目关联不表示商品已经合规；关系确认不触发商品审批或处置。</p><PathPanel v-if="graph" :nodes="graph.nodes" :case-id="caseId" :as-of="asOf" @linked="load" /></section></template>
