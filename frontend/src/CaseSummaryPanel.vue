<script setup lang="ts">
import {onUnmounted,ref,watch} from 'vue'
import {api,reportError,type Case} from './api'
const props=defineProps<{item:Case}>()
type Summary={version:string;content_sha256:string;case:{id:string;revision:number;status:string};case_fields:Record<string,unknown>;business_scope:Record<string,unknown>;unresolved:{missing_fields:string[];conflicts:unknown[];unknown_record_ids:string[];case_needs_info:boolean};evidence_refs:{id:string;branch:string;sha256:string;location:Record<string,unknown>}[];completed_actions:{id:string;kind:string;target_id:string;case_revision:number}[];truncated:{evidence_refs:boolean;completed_actions:boolean}}
const value=ref<Summary|null>(null),busy=ref(false)
let epoch=0
onUnmounted(()=>{epoch++})
async function load(){
 const current=++epoch;busy.value=true;value.value=null
 try{const result=await api<Summary>(`/cases/${props.item.id}/summary`);if(current===epoch)value.value=result}
 catch(cause){if(current===epoch)reportError(cause,'案件摘要读取失败')}
 finally{if(current===epoch)busy.value=false}
}
watch(()=>`${props.item.id}:${props.item.merchant_id}:${props.item.revision}:${props.item.status}`,load,{immediate:true})
</script>
<template><section class="panel"><div class="panel-heading"><h2>案件结构化摘要</h2><button class="secondary" :disabled="busy" @click="load">刷新摘要</button></div><p class="muted">读取时的派生视图，有来源和版本；声明不等于认证，摘要不能代替当前证据或审批许可。</p><p v-if="busy" role="status">正在核对摘要来源…</p>
<template v-if="value"><p class="muted">{{value.version}} · 案件v{{value.case.revision}}</p><details><summary>当前字段与业务范围</summary><pre class="event-json">{{JSON.stringify({declarations:value.case_fields,business_scope:value.business_scope},null,2)}}</pre></details>
<p v-if="value.unresolved.case_needs_info">当前案件仍需补充资料。</p><p>未解决字段：{{value.unresolved.missing_fields.join('、')||'本次确定性检查未发现字段缺失，仍需取证核验'}}</p><p v-if="value.unresolved.unknown_record_ids.length">范围未明确的批次：{{value.unresolved.unknown_record_ids.join('、')}}</p><details v-if="value.unresolved.conflicts.length"><summary>查看字段冲突</summary><pre class="event-json">{{JSON.stringify(value.unresolved.conflicts,null,2)}}</pre></details>
<details><summary>当前来源引用（{{value.evidence_refs.length}}）</summary><article v-for="source in value.evidence_refs" :key="source.id" class="rule-card"><strong>{{source.id}}</strong><p class="source-location">{{JSON.stringify(source.location)}}</p><small class="source-location">来源指纹 {{source.sha256}}</small></article><p v-if="!value.evidence_refs.length">本次范围没有可引用来源，请补充资料或查看规则。</p></details>
<details><summary>已完成的本地动作（{{value.completed_actions.length}}）</summary><p v-for="action in value.completed_actions" :key="action.id">{{action.kind}} · {{action.target_id}} · 原案件v{{action.case_revision}}</p><p v-if="!value.completed_actions.length">暂无可核对的已执行处置回执；模型建议不计为已完成。</p></details>
<p v-if="value.truncated.evidence_refs||value.truncated.completed_actions" class="notice">摘要展示范围已截断，不能推断这是全部来源或历史动作。</p><details><summary>查看摘要版本指纹</summary><small class="source-location">{{value.content_sha256}}</small></details></template></section></template>
