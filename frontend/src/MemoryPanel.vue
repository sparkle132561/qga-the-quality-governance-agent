<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import {actor,api,error,reportError,type Case} from './api'
const props=defineProps<{item:Case}>()
type Reference={id:string;quote:string;product_id:string;source_case_id:string;source_id:string}
const references=ref<Reference[]>([]),busy=ref(false),sourceId=ref(''),productId=ref(''),quote=ref(''),note=ref(''),notice=ref(''),truncated=ref(false)
const expires=ref(new Date(Date.now()+30*86400000).toISOString().slice(0,10))
const sources=computed(()=>[...new Map(Object.entries(props.item.latest_analysis?.branches||{}).filter(([name])=>name!=='history').flatMap(([,branch])=>branch.evidence).filter(hit=>/^(rule|material|product|batch|event|work-order):/.test(hit.id)).map(hit=>[hit.id,hit])).values()])
const subjects=computed(()=>[...new Set([typeof props.item.payload.sku_id==='string'?props.item.payload.sku_id:'',...sources.value.map(hit=>hit.id.startsWith('product:')?hit.id.slice(8):typeof hit.location.product_id==='string'?hit.location.product_id:'')].filter(Boolean))])
const selected=computed(()=>sources.value.find(hit=>hit.id===sourceId.value))
const eligible=computed(()=>['approved','confirmed'].includes(props.item.status)&&actor.value?.role!=='admin')
let epoch=0
onUnmounted(()=>{epoch++})
async function load(){const current=++epoch;references.value=[];truncated.value=false;try{const result=await api<{items:Reference[];scan_truncated:boolean}>(`/cases/${props.item.id}/reference-memories`);if(current===epoch){references.value=result.items;truncated.value=result.scan_truncated}}catch(cause){if(current===epoch)reportError(cause,'参考记忆读取失败')}}
onMounted(load)
watch(()=>`${props.item.id}:${props.item.revision}:${props.item.latest_analysis?.run_id||''}:${props.item.status}`,()=>{sourceId.value='';productId.value='';quote.value='';note.value='';notice.value='';load()})
async function propose(){
  busy.value=true;error.value='';notice.value=''
  try{await api('/memories',{method:'POST',body:JSON.stringify({source_case_id:props.item.id,product_id:productId.value,source_id:sourceId.value,quote:quote.value,expires_on:expires.value,note:note.value})});notice.value='已保存候选，审核员核验后才能用于后续取证。';quote.value='';note.value=''}
  catch(cause){reportError(cause,'记忆候选保存失败')}finally{busy.value=false}
}
</script>
<template><section class="panel"><div class="panel-heading"><h2>已核验历史参考</h2><router-link to="/memories">管理候选与有效期 →</router-link></div><p class="muted">按当前商家、商品和适用日期选择。历史参考不能覆盖当前事实或替代本次原文取证。</p><article v-for="row in references" :key="row.id" class="rule-card"><p class="memory-quote">{{row.quote}}</p><small>{{row.product_id}} · {{row.source_id}}</small><router-link :to="`/cases/${row.source_case_id}`">查看原案件</router-link></article><p v-if="!references.length" class="empty small">当前没有有效且相关的已核验参考。</p>
  <p v-if="truncated" class="alert">历史参考检查已达上限，结果可能不完整。</p><details v-if="eligible && sources.length && subjects.length"><summary>从已批准来源提出记忆候选</summary><form class="search-form" @submit.prevent="propose"><label>适用商品<select v-model="productId" required><option value="">选择商品</option><option v-for="id in subjects" :key="id" :value="id">{{id}}</option></select></label><label>批准来源<select v-model="sourceId" required><option value="">选择来源</option><option v-for="source in sources" :key="source.id" :value="source.id">{{source.id}}</option></select></label><details v-if="selected"><summary>核对原文与位置</summary><small>{{JSON.stringify(selected.location)}}</small><p class="memory-quote">{{selected.text}}</p></details><label>精确原文摘录<textarea v-model="quote" required minlength="10" maxlength="500" rows="3"></textarea></label><label>摘录适用说明<textarea v-model="note" required maxlength="500" rows="2"></textarea></label><label>有效期（最长90天）<input v-model="expires" required type="date"></label><button class="secondary" :disabled="busy">保存记忆候选</button></form></details><p v-if="notice" class="notice" role="status">{{notice}}</p>
</section></template>
