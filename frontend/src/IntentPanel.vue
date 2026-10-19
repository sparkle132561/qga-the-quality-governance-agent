<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import {api,reportError,type Case,workflowLabels} from './api'
const props=defineProps<{item:Case}>()
type Proposal={workflow:string;intent:string;entities:Record<string,{value:string;source:string}>;missing_fields:string[];human_review_required:boolean;confidence:number;token:string;engine_mode:string}
type Confirmation={destination:string;message:string;business_action_executed:boolean}
const question=ref(''),busy=ref(false),proposal=ref<Proposal|null>(null),confirmation=ref<Confirmation|null>(null)
let epoch=0
function invalidate(){epoch++;proposal.value=null;confirmation.value=null;busy.value=false}
watch(question,invalidate)
watch(()=>`${props.item.id}:${props.item.revision}:${props.item.status}`,invalidate)
onUnmounted(()=>{epoch++})
const labels:Record<string,string>={create_review:'新建准入',submit_materials:'提交资料',check_certificate:'核对证书',check_fields:'核对字段',query_requirements:'查询准入要求',query_case:'查询案件',import_incident:'导入事件',analyze_impact:'分析事件影响',explain_risk:'解释风险',query_affected_products:'查询影响商品',query_affected_orders:'查询影响订单',inspect_evidence:'查看证据',submit_remediation:'提交整改',submit_appeal:'提交申诉',query_work_order:'查询工单',request_review:'请求复核',approve:'请求批准',reject:'请求拒绝',close:'请求关闭',escalate:'请求升级',search_rules:'检索规则',search_cases:'查询案件列表',provide_missing_fields:'补充字段',request_human_help:'请求人工帮助',out_of_scope:'超出支持范围'}
const slots:Record<string,string>={case_id:'当前案件',sku_id:'商品',event_id:'质量事件',work_order_id:'工单',model:'型号',as_of:'适用日期',merchant_id:'所属商家',batch_number:'批次号'}
function missing(value:string){const key=value.replace(/_(conflict|invalid)$/,'');return value==='workflow_conflict'?'意图流程与当前案件不一致，请选择对应案件':value==='supported_intent'?'请说明商品质量治理相关需求':`${slots[key]||key}${value.endsWith('_conflict')?'与当前案件冲突':value.endsWith('_invalid')?'需使用有效ISO日期':'尚未提供，请补充到问题或案件字段中'}`}
const target=computed(()=>{
  const kind=confirmation.value?.destination
  if(kind==='workspace')return '/'
  if(kind==='incidents')return '/incidents'
  if(kind==='work_orders')return '/work-orders'
  return `/cases/${props.item.id}#intent-${kind||'case'}`
})
async function parse(){
  const current=++epoch;busy.value=true;proposal.value=null;confirmation.value=null
  try{const value=await api<Proposal>(`/cases/${props.item.id}/intent`,{method:'POST',body:JSON.stringify({question:question.value})});if(current===epoch)proposal.value=value}
  catch(cause){if(current===epoch)reportError(cause,'意图解析失败，请检查模型配置或重新表达')}
  finally{if(current===epoch)busy.value=false}
}
async function confirm(){
  if(!proposal.value)return
  const current=++epoch;busy.value=true
  try{const value=await api<Confirmation>(`/cases/${props.item.id}/intent/confirm`,{method:'POST',body:JSON.stringify({token:proposal.value.token})});if(current===epoch)confirmation.value=value}
  catch(cause){if(current===epoch)reportError(cause,'无法确认，请核对权限、状态或重新解析')}
  finally{if(current===epoch)busy.value=false}
}
</script>
<template><section class="panel"><h2>自然语言任务入口</h2><p class="muted">只处理当前案件。先识别意图与字段，再确认进入业务工作区；不会自动批准或执行处置。请用ISO日期，例如2025-07-01。</p>
<form class="search-form" @submit.prevent="parse"><label>你想处理什么？<textarea v-model="question" required maxlength="2000" rows="3" placeholder="例如：请查询2025-07-01适用的准入规则"></textarea></label><button class="secondary" :disabled="busy||!question.trim()">{{busy?'正在处理…':'识别意图与字段'}}</button></form>
<div v-if="proposal" class="notice"><h3>{{labels[proposal.intent]||proposal.intent}}</h3><p>流程：{{workflowLabels[proposal.workflow]||proposal.workflow}} · 模式：{{proposal.engine_mode}}</p><dl class="facts"><template v-for="(slot,key) in proposal.entities" :key="key"><dt>{{slots[key]||key}}</dt><dd>{{slot.value}} · {{slot.source==='question'?'来自原问题':'来自当前案件'}}</dd></template></dl><p class="muted">置信度{{proposal.confidence}}仅为模型自报值，不能代替核验或授权。</p>
<ul v-if="proposal.missing_fields.length"><li v-for="field in proposal.missing_fields" :key="field">{{missing(field)}}</li></ul><p v-if="proposal.missing_fields.length">请修改问题或补齐案件字段后重新识别。</p>
<button v-else class="primary" :disabled="busy||!!confirmation" @click="confirm">确认意图，进入对应工作区</button>
</div><div v-if="confirmation" class="notice" role="status"><p>{{confirmation.message}}</p><router-link :to="target">打开对应工作区 →</router-link><p>批准、下架、提交或关闭等业务动作仍需明确操作；此处未执行。</p></div></section></template>
