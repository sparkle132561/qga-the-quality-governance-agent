<script setup lang="ts">
import {ref} from 'vue'
import {api,ApiError,error,reportError} from './api'
const props=defineProps<{caseId:string}>()
type Advice={recommendation:string;explanation:string;evidence_ids:string[];missing_fields:string[]}
type Event={event:string;name?:string;status?:string;code?:string;elapsed_ms?:number;plan?:{reason:string;disabled:string[]}}
type Result={thread_id:string;status:string;advice:Advice;engine_mode:string;trace:Event[]}
const question=ref('当前资料是否足够？请检索规则并说明证据缺口。')
const routing=ref('adaptive')
const asOf=ref('2025-07-01'),busy=ref(false),result=ref<Result|null>(null),events=ref<Event[]>([])
const runId=ref(''),requestId=ref(''),cancelling=ref(false),cancelNotice=ref('')
async function ask(){
  busy.value=true;result.value=null;events.value=[];runId.value='';error.value='';requestId.value=crypto.randomUUID();cancelNotice.value=''
  try{
    result.value=await api<Result>(`/cases/${props.caseId}/assistant`,{method:'POST',body:JSON.stringify({question:question.value,as_of:asOf.value,client_request_id:requestId.value,routing:routing.value})})
    events.value=result.value.trace;runId.value=result.value.thread_id
  }catch(cause){
    if(cause instanceof ApiError && cause.detail && typeof cause.detail==='object' && 'code' in cause.detail && cause.detail.code==='analysis_cancelled')cancelNotice.value='助手取消已生效。';else reportError(cause,'助手取证失败')
    if(cause instanceof ApiError && cause.detail && typeof cause.detail==='object' && 'thread_id' in cause.detail){
      runId.value=String(cause.detail.thread_id)
      try{events.value=(await api<{trace:Event[]}>(`/cases/${props.caseId}/agent-runs/${encodeURIComponent(runId.value)}`)).trace}catch{}
    }
  }finally{busy.value=false}
}
async function cancel(){
  cancelling.value=true
  try{await api(`/cases/${props.caseId}/assistant-requests/${requestId.value}/cancel`,{method:'POST'});cancelNotice.value='已请求取消，等待已发出的调用结束。'}catch(cause){reportError(cause,'助手取消失败')}finally{cancelling.value=false}
}
</script>
<template><section class="panel"><h2>案件取证助手</h2><p class="muted">读取当前案件与适用规则，按需调用检索工具。建议不执行审批或处置。</p><form class="search-form" @submit.prevent="ask"><label>你的问题<textarea v-model="question" required maxlength="2000" rows="3"></textarea></label><label>取证策略<select v-model="routing"><option value="adaptive">按问题自主择路</option><option value="all">全部可用增强（对照）</option><option value="fixed">固定词法检索</option></select></label><label>规则适用日期<input v-model="asOf" type="date" required></label><button class="secondary" :disabled="busy">{{busy?'取证中…':'取证并解释（需Agnes Key）'}}</button></form><button v-if="busy" class="secondary" :disabled="cancelling" @click="cancel">取消助手取证</button><p v-if="cancelNotice" class="notice" role="status">{{cancelNotice}}</p><div v-if="result" class="notice"><strong>{{result.advice.recommendation}}</strong><p>{{result.advice.explanation}}</p><p v-if="result.advice.missing_fields.length">待补充：{{result.advice.missing_fields.join('、')}}</p><small>模式 {{result.engine_mode}} · {{result.advice.evidence_ids.length}} 条引用</small></div><details v-if="runId" class="agent-trace" open><summary>查看运行记录</summary><small class="source-location">{{runId}}</small><ol><li v-for="(event,index) in events" :key="index"><strong>{{event.event}}</strong><span v-if="event.plan"> · {{event.plan.reason}}<small v-if="event.plan.disabled.length">不可用：{{event.plan.disabled.join("、")}}</small></span><span v-if="event.name"> · {{event.name}}</span><span v-if="event.status"> · {{event.status}}</span><span v-if="event.code"> · {{event.code}}</span><small v-if="event.elapsed_ms !== undefined">{{event.elapsed_ms.toFixed(1)}} ms</small></li></ol></details></section></template>
