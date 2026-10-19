<script setup lang="ts">
import {reactive,ref} from 'vue'
import {api,error,reportError} from './api'
type Quote={quote:string;char_start?:number|null;char_end?:number|null}
type Proposal={event_type:string;brand:string|null;models:string[];production_scope:string;production_start:string|null;production_end:string|null;batches:string[];serial_scope_original:string|null;defect_summary:string|null;field_evidence:Record<string,Quote>}
const props=defineProps<{id:string;revision:number;seed:Proposal}>()
const emit=defineEmits<{confirmed:[]}>()
const fields=reactive(JSON.parse(JSON.stringify(props.seed)) as Proposal)
const modelText=ref(fields.models.join('\n')),batchText=ref(fields.batches.join('\n')),note=ref(''),busy=ref(false)
const sourceFields=[['event_type','事件类型'],['brand','品牌'],['models','完整型号'],['production_scope','全部日期声明（如适用）'],['production_start','生产时间起点'],['production_end','生产时间终点'],['batches','批次'],['serial_scope_original','序列号范围'],['defect_summary','缺陷原文']] as const
for(const [key] of sourceFields)fields.field_evidence[key]??={quote:''}
function quoteChanged(key:string){fields.field_evidence[key]={quote:fields.field_evidence[key]?.quote||''}}
async function confirm(){
  busy.value=true;error.value=''
  try{
    const split=(value:string)=>value.split(/[\n,，]+/).map(item=>item.trim()).filter(Boolean)
    const body={...fields,models:split(modelText.value),batches:split(batchText.value),field_evidence:Object.fromEntries(Object.entries(fields.field_evidence).filter(([,source])=>source.quote.trim()))}
    await api(`/incidents/drafts/${props.id}/confirm`,{method:'POST',body:JSON.stringify({expected_revision:props.revision,proposal:body,note:note.value})})
    emit('confirmed')
  }catch(cause){reportError(cause,'字段确认失败')}finally{busy.value=false}
}
</script>
<template><form class="search-form" @submit.prevent="confirm"><h3>核对事件范围</h3><div class="form-grid"><label>事件类型<select v-model="fields.event_type"><option value="unknown">未知</option><option value="recall">召回</option><option value="inspection">抽检</option></select></label><label>品牌<input v-model="fields.brand" maxlength="100"></label></div><label>完整型号（每行一项）<textarea v-model="modelText" rows="2"></textarea></label><label>生产时间范围<select v-model="fields.production_scope"><option value="unknown">未知，不当作全部日期</option><option value="range">原文有明确起止范围</option><option value="all">原文明说全部生产日期</option></select></label><div class="form-grid"><label>生产起点<input v-model="fields.production_start" placeholder="YYYY-MM 或 YYYY-MM-DD"></label><label>生产终点<input v-model="fields.production_end" placeholder="保持与原文相同精度"></label></div><label>明确批次（每行一项，缺失留空）<textarea v-model="batchText" rows="2"></textarea></label><label>序列号范围原文<input v-model="fields.serial_scope_original" maxlength="300"></label><label>缺陷原文片段<textarea v-model="fields.defect_summary" rows="2" maxlength="300"></textarea></label><details class="rule-card"><summary>逐字段核对来源引文</summary><p class="muted">从公告复制原文；引文重复时复制更完整的上下文。未知字段可以留空，不能补造范围。</p><label v-for="[key,label] in sourceFields" :key="key">{{label}}的来源<textarea v-model="fields.field_evidence[key]!.quote" rows="2" maxlength="300" @input="quoteChanged(key)"></textarea></label></details><label>字段核验说明<textarea v-model="note" required rows="2" maxlength="500" placeholder="说明已核对的字段，以及仍然未知的限制"></textarea></label><button class="primary" :disabled="busy">确认范围字段（不执行处置）</button></form></template>
