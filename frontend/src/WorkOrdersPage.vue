<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { actor, api, error, reportError } from './api'
type WorkOrder = {id:string;case_id:string;merchant_id:string;status:string;revision:number;assigned_to:string;product_ids:string[];batch_ids:string[];active_review_case_id:string|null;closure_note:string|null;created_at:string;is_demo:boolean}
const router=useRouter(),orders=ref<WorkOrder[]>([]),selected=ref<WorkOrder|null>(null)
const busy=ref(false),loading=ref(true),kind=ref('remediation'),statement=ref(''),asOf=ref('')
const labels:Record<string,string>={open:'待整改',in_progress:'整改中',waiting_review:'待复核',escalated:'已升级跟进',closed:'已关闭'}
async function load(){try{orders.value=await api<WorkOrder[]>('/work-orders')}catch(cause){reportError(cause,'工单读取失败')}finally{loading.value=false}}
onMounted(load)
async function create(){
  if(!selected.value)return
  busy.value=true;error.value=''
  try{
    const result=await api<{case_id:string}>(`/work-orders/${selected.value.id}/review-cases`,{method:'POST',body:JSON.stringify({expected_revision:selected.value.revision,review_kind:kind.value,statement:statement.value,as_of:asOf.value})})
    await router.push(`/cases/${result.case_id}`)
  }catch(cause){reportError(cause,'创建复核失败');await load();selected.value=null}
  finally{busy.value=false}
}
</script>
<template>
  <main class="page">
    <div class="page-heading"><div><p class="eyebrow">REMEDIATION / DEMO</p><h1>整改与申诉工单</h1><p class="muted">事件处置后由所属商家提交新证据，审核员决定关闭或升级跟进。关闭后仍需重新准入审核才能上架。</p></div><button class="secondary" :disabled="busy" @click="load">刷新工单</button></div>
    <p v-if="loading" role="status">正在读取工单…</p>
    <p v-else-if="!orders.length" class="empty">暂无可见工单。批准事件影响分析并执行模拟处置后，将生成整改工单。</p>
    <article v-for="row in orders" :key="row.id" class="panel">
      <div class="panel-heading"><h2>{{labels[row.status]||row.status}}</h2><span class="badge">工单 v{{row.revision}} · {{row.is_demo?'模拟':'业务记录'}}</span></div>
      <p>{{row.id}}</p><p>责任人：{{row.assigned_to}} · {{row.merchant_id}}</p><p>商品：{{row.product_ids.join('、')}} · 批次：{{row.batch_ids.join('、')}}</p>
      <div class="action-bar"><router-link :to="`/cases/${row.case_id}`">原事件与处置时间线</router-link><router-link v-if="row.active_review_case_id" :to="`/cases/${row.active_review_case_id}`">当前复核与证据</router-link><button v-if="actor?.role==='merchant' && ['open','escalated'].includes(row.status)" class="primary" :disabled="busy" @click="selected=row;statement='';asOf=''">提交整改或申诉</button></div>
      <p v-if="row.closure_note" class="notice">关闭说明：{{row.closure_note}}</p>
      <form v-if="selected?.id===row.id" class="search-form" @submit.prevent="create">
        <label>复核类型<select v-model="kind"><option value="remediation">整改复核</option><option value="appeal">误判申诉</option></select></label>
        <label>证据适用日期<input v-model="asOf" type="date" required></label>
        <label>整改说明或申诉理由<textarea v-model="statement" required maxlength="3000" rows="4"></textarea></label>
        <p class="muted">创建后请上传新材料再提交。说明和材料自述仍须人工核验。</p>
        <div class="action-bar"><button class="primary" :disabled="busy||!statement.trim()">创建复核草稿</button><button type="button" class="secondary" :disabled="busy" @click="selected=null">取消</button></div>
      </form>
    </article>
  </main>
</template>
