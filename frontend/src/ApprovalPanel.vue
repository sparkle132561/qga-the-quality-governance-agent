<script setup lang="ts">
import {onMounted,ref,watch} from 'vue'
import {actor,api,error,reportError,type Case} from './api'
type Approval={id:string;revision:number;analysis_run_id:string;decision:string;status:string;actor_id:string;note:string;targets:{product_ids:string[];batch_ids:string[];order_ids:string[];work_order_id?:string|null};executed_at:string|null}
type Effect={id:string;kind:string;target_id:string;details:Record<string,unknown>;is_demo:boolean}
const props=defineProps<{item:Case}>(),emit=defineEmits<{updated:[]}>()
const approvals=ref<Approval[]>([]),effects=ref<Effect[]>([]),note=ref(''),busy=ref(false)
const labels:Record<string,string>={simulate_delist:'模拟下架',simulate_notification:'模拟通知账本',create_work_order:'创建整改工单',simulate_listing:'模拟上架',close_work_order:'关闭整改工单'}
async function load(){try{[approvals.value,effects.value]=await Promise.all([api<Approval[]>(`/cases/${props.item.id}/approvals`),api<Effect[]>(`/cases/${props.item.id}/effects`)])}catch(cause){reportError(cause,'审核记录读取失败')}}
watch(()=>props.item.latest_analysis?.run_id,load)
onMounted(load)
async function decide(decision:string){
  busy.value=true;error.value=''
  try{await api(`/cases/${props.item.id}/decision`,{method:'POST',body:JSON.stringify({expected_revision:props.item.revision,analysis_run_id:props.item.latest_analysis?.run_id,decision,note:note.value})});await load();emit('updated')}
  catch(cause){reportError(cause,'审核决定失败')}finally{busy.value=false}
}
async function execute(row:Approval){
  busy.value=true;error.value=''
  try{await api(`/cases/${props.item.id}/approvals/${row.id}/execute`,{method:'POST',body:JSON.stringify({expected_revision:props.item.revision,analysis_run_id:row.analysis_run_id,simulation_only:true})});await load();emit('updated')}
  catch(cause){reportError(cause,'模拟处置失败')}finally{busy.value=false}
}
async function revoke(row:Approval){
  busy.value=true;error.value=''
  try{await api(`/cases/${props.item.id}/approvals/${row.id}/revoke`,{method:'POST'});await load();emit('updated')}
  catch(cause){reportError(cause,'决定撤回失败')}finally{busy.value=false}
}
async function withdraw(){
  busy.value=true;error.value=''
  try{await api(`/cases/${props.item.id}/withdraw-analysis`,{method:'POST',body:JSON.stringify({expected_revision:props.item.revision,analysis_run_id:props.item.latest_analysis?.run_id})});emit('updated')}
  catch(cause){reportError(cause,'待审分析撤回失败')}finally{busy.value=false}
}
function current(row:Approval){return row.revision===props.item.revision&&row.analysis_run_id===props.item.latest_analysis?.run_id}
</script>
<template><section class="panel"><h2>人工审核与模拟处置</h2><p class="muted">决定绑定当前资料与分析。模拟处置只写本地上架状态、通知账本和工单。</p><form v-if="actor?.role==='reviewer' && item.status==='waiting_review'" class="search-form" @submit.prevent="decide('approve')"><label>审核决定说明<textarea v-model="note" required maxlength="500" rows="3"></textarea></label><div class="action-bar"><button v-if="item.latest_analysis?.recommendation==='review_ready'" class="primary" :disabled="busy">批准此版本</button><button type="button" class="secondary" :disabled="busy||!note.trim()" @click="decide('reject')">拒绝此版本</button><button type="button" class="secondary" :disabled="busy||!note.trim()" @click="decide('request_info')">要求补件</button><button type="button" class="secondary" :disabled="busy" @click="withdraw">撤回待审分析并重跑</button></div></form><article v-for="row in approvals" :key="row.id" class="rule-card"><strong>{{row.decision}} · {{row.status}} · v{{row.revision}}</strong><small>{{row.actor_id}} · {{row.analysis_run_id}}</small><p>{{row.note}}</p><p class="source-location">批准目标：<span v-if="row.targets.work_order_id">工单 {{row.targets.work_order_id}} · </span>{{row.targets.product_ids.join('、')||'无命中商品'}} · {{row.targets.batch_ids.length}}个批次 · {{row.targets.order_ids.length}}个订单</p><div v-if="actor?.role==='reviewer' && current(row) && row.status==='active' && !row.executed_at" class="action-bar"><button class="primary" :disabled="busy" @click="execute(row)">执行已批准模拟处置</button><button class="secondary" :disabled="busy" @click="revoke(row)">撤回此决定并重分析</button></div><p v-if="row.executed_at" class="muted">已执行模拟处置；重复请求返回同一收据。</p><p v-else-if="!current(row)" class="muted">旧资料或旧分析版本，不能执行。</p></article><div v-if="effects.length" class="notice"><strong>模拟处置收据</strong><article v-for="effect in effects" :key="effect.id" class="rule-card"><p>{{labels[effect.kind]||effect.kind}} · {{effect.target_id}}</p><details><summary>查看本地收据</summary><pre class="event-json">{{JSON.stringify(effect.details,null,2)}}</pre><small>{{effect.id}} · {{effect.is_demo?'模拟':'非模拟记录'}}</small></details></article></div><p v-else class="muted">尚未执行任何处置。</p></section></template>
