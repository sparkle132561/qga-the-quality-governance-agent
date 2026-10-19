<script setup lang="ts">
import {onUnmounted,ref,watch} from 'vue'
import {actor,api,error,reportError} from './api'
type Node={id:string;kind:string;label:string}
type Path={nodes:Node[];hops:number;edges:{predicate:string;origin:string;traversed_reverse:boolean;provenance:Record<string,unknown>}[]}
type Result={paths:Path[];truncated:boolean;stop_reason:string;timing_ms:Record<string,number>;evidence:{id:string;location:Record<string,unknown>;text:string}[]}
const props=defineProps<{nodes:Node[];caseId?:string;asOf:string}>()
const emit=defineEmits<{linked:[]}>()
const start=ref(''),end=ref(''),kind=ref('order'),direction=ref('out'),busy=ref(false)
const left=ref(''),right=ref(''),note=ref(''),result=ref<Result|null>(null)
let requestRevision=0
watch([()=>props.asOf,()=>props.caseId,()=>props.nodes],()=>{
  requestRevision++;result.value=null;busy.value=false
  const visible=new Set(props.nodes.map(node=>node.id))
  for(const field of [start,end,left,right])if(!visible.has(field.value))field.value=''
})
onUnmounted(()=>{requestRevision++})
async function paths(){
  busy.value=true;error.value='';result.value=null
  const revision=++requestRevision
  try{
    const response=await api<Result>('/graph/paths',{method:'POST',body:JSON.stringify({as_of:props.asOf,case_id:props.caseId,start_id:start.value,end_id:end.value||undefined,target_kind:end.value?undefined:kind.value||undefined,direction:direction.value,max_hops:3,limit:10})})
    if(revision===requestRevision)result.value=response
  }catch(cause){if(revision===requestRevision)reportError(cause,'路径检索失败')}
  finally{if(revision===requestRevision)busy.value=false}
}
async function link(){
  busy.value=true;error.value=''
  try{
    await api('/graph/identity-links',{method:'POST',body:JSON.stringify({as_of:props.asOf,case_id:props.caseId,subject:left.value,object:right.value,note:note.value})})
    result.value=null;emit('linked')
  }catch(cause){reportError(cause,'身份关联失败')}finally{busy.value=false}
}
</script>
<template><div class="rule-card"><h3>有限多跳取证</h3><p class="muted">最多三跳；先指定可见实体。路径回答关系问题，风险判断仍需核对事件范围和规则。</p><form class="search-form" @submit.prevent="paths"><label>路径起点<select v-model="start" required><option value="" disabled>选择实体</option><option v-for="node in nodes" :key="node.id" :value="node.id">{{node.label}} · {{node.id}}</option></select></label><label>路径终点（可选）<select v-model="end"><option value="">不限定具体终点</option><option v-for="node in nodes" :key="node.id" :value="node.id">{{node.label}} · {{node.id}}</option></select></label><div class="form-grid"><label>终点类型<select v-model="kind"><option value="">全部类型</option><option value="order">订单</option><option value="batch">批次</option><option value="product">商品</option><option value="supplier">供应商</option><option value="rule">规则</option></select></label><label>遍历方向<select v-model="direction"><option value="out">沿关系方向</option><option value="in">逆关系方向</option><option value="both">两个方向</option></select></label></div><button class="secondary" :disabled="busy">检索三跳路径</button></form><template v-if="result"><p class="source-location">{{result.paths.length}} 条路径 · {{result.timing_ms.total?.toFixed(1)}} ms · {{result.truncated?'已截断：'+result.stop_reason:'遍历完成'}}</p><p v-if="!result.paths.length" class="muted">当前预算和方向内没有路径。若已截断，不能据此断言不存在关联。</p><article v-for="(path,index) in result.paths" :key="index" class="rule-card"><strong>{{path.nodes.map(node=>node.label).join(' → ')}}</strong><small class="source-location">{{path.nodes.map(node=>node.id).join(' → ')}}</small><details><summary>查看路径依据 · {{path.hops}} 跳</summary><p v-for="(edge,edgeIndex) in path.edges" :key="edgeIndex">{{edge.predicate}} · {{edge.traversed_reverse?'逆向遍历':'原始方向'}} · {{edge.origin}}<br>{{JSON.stringify(edge.provenance)}}</p></details></article><details v-for="hit in result.evidence" :key="hit.id"><summary>路径原文 · {{JSON.stringify(hit.location)}}</summary><p>{{hit.text}}</p></details></template></div><details v-if="actor?.role!=='merchant'" class="rule-card"><summary>核验同一实体的来源身份</summary><p class="muted">仅关联文档来源与另一来源实体。同型号不代表同SKU，两个独立业务记录禁止直接合并。</p><form class="search-form" @submit.prevent="link"><label>身份来源一<select v-model="left" required><option value="" disabled>选择来源实体</option><option v-for="node in nodes" :key="node.id" :value="node.id">{{node.label}} · {{node.id}}</option></select></label><label>身份来源二<select v-model="right" required><option value="" disabled>选择来源实体</option><option v-for="node in nodes" :key="node.id" :value="node.id">{{node.label}} · {{node.id}}</option></select></label><label>身份核验理由<textarea v-model="note" required maxlength="500" rows="2" placeholder="核对主体、型号、批次或证书主体；不要只凭同名"></textarea></label><button class="secondary" :disabled="busy">确认来源身份关联</button></form></details></template>
