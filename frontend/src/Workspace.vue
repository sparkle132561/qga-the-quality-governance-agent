<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { actor, api, error, reportError, statusLabels, workflowLabels, type Case } from './api'
import CaseFields from './CaseFields.vue'
const cases = ref<Case[]>([])
const merchants = ref<{id:string;name:string}[]>([])
const loading = ref(false)
const creating = ref(false)
const showForm = ref(false)
const title = ref('')
const workflow = ref('qualification')
const owner = ref('merchant-a')
const payload = ref<Record<string,unknown>>({as_of:'2025-07-01'})
const offset = ref(0)
let loadEpoch=0
onUnmounted(()=>{loadEpoch++})
async function load() {
  const current=++loadEpoch, pageOffset=offset.value
  loading.value = true
  try { const [items,owners]=await Promise.all([api<Case[]>(`/cases?limit=50&offset=${pageOffset}`),api<{id:string;name:string}[]>('/merchants')]);if(current===loadEpoch&&pageOffset===offset.value){cases.value=items;merchants.value=owners} }
  catch (cause) { if(current===loadEpoch)reportError(cause, '加载失败') }
  finally { if(current===loadEpoch)loading.value = false }
}
onMounted(load)
async function create() {
  error.value = ''; creating.value = true
  try {
    await api('/cases', { method: 'POST', body: JSON.stringify({ title: title.value, workflow: workflow.value, merchant_id: actor.value?.role === 'merchant' ? undefined : owner.value, payload: payload.value }) })
    showForm.value = false; title.value = ''; offset.value=0; await load()
  } catch (cause) { reportError(cause, '创建失败') }
  finally { creating.value = false }
}
async function page(delta:number){offset.value=Math.max(0,offset.value+delta);await load()}
</script>

<template>
  <main class="page">
    <section class="page-heading"><div><p class="eyebrow">CASE WORKSPACE</p><h1>案件工作台</h1><p class="muted">{{ actor?.role === 'merchant' ? '提交商品资料，跟踪审核与补件进度。' : '查看案件状态、分析结果及原始证据。' }}</p></div><button class="primary" @click="showForm = !showForm">{{ showForm ? '收起表单' : '＋ 新建案件' }}</button></section>
    <section class="metrics"><div><small>当前列表</small><strong>{{ cases.length }}</strong><span>最多展示最近 50 条</span></div><div><small>待处理</small><strong>{{ cases.filter(c => ['submitted','waiting_review'].includes(c.status)).length }}</strong><span>已提交 / 待审核</span></div><div><small>待补充</small><strong>{{ cases.filter(c => c.status === 'needs_info').length }}</strong><span>补齐证据后重新提交</span></div></section>
    <form v-if="showForm" class="panel form-panel" @submit.prevent="create">
      <h2>新建演示案件</h2><div class="form-grid"><label>案件标题<input v-model="title" required maxlength="200" placeholder="例如：K170 水壶准入资料审核"></label><label>业务流程<select v-model="workflow"><option value="qualification">准入审核</option><option value="incident">质量事件影响</option></select></label><label v-if="actor?.role !== 'merchant'">所属商家<select v-model="owner" required><option v-for="merchant in merchants" :key="merchant.id" :value="merchant.id">{{ merchant.name }}</option></select></label></div>
      <CaseFields v-model="payload" :workflow="workflow" :merchant-id="actor?.merchant_id||owner" /><p class="muted">保存草稿后上传原材料。整改与申诉请从<router-link to="/work-orders">已有工单</router-link>发起。</p><button class="primary" :disabled="creating">{{ creating ? '正在创建…' : '保存草稿' }}</button>
    </form>
    <section class="panel"><div class="panel-heading"><h2>案件列表</h2><button class="secondary" :disabled="loading" @click="load">刷新</button></div>
      <div v-if="loading" class="empty" role="status">正在加载案件…</div>
      <div v-else-if="!cases.length" class="empty"><span class="empty-icon">↗</span><h3>{{offset?'本页没有更多案件':'还没有案件'}}</h3><p>创建一份演示案件，开始记录资料和审核过程。</p></div>
      <div v-else class="table-wrap"><table><thead><tr><th>案件</th><th>流程</th><th>商家</th><th>状态</th><th>资料版本</th><th></th></tr></thead><tbody><tr v-for="item in cases" :key="item.id"><td><strong>{{ item.title }}</strong><small class="mono">{{ item.id.slice(0,8) }}</small></td><td>{{ workflowLabels[item.workflow] }}</td><td>{{ item.merchant_id }}</td><td><span class="status" :class="item.status">{{ statusLabels[item.status] }}</span></td><td>v{{ item.revision }}</td><td><router-link :to="`/cases/${item.id}`" class="detail-link">查看 →</router-link></td></tr></tbody></table></div>
    </section>
    <div class="action-bar"><button class="secondary" :disabled="loading||offset===0" @click="page(-50)">上一页</button><span class="muted">第{{Math.floor(offset/50)+1}}页 · 每页最多50条</span><button class="secondary" :disabled="loading||cases.length<50" @click="page(50)">下一页</button></div>
    <div class="notice"><strong>本地演示与核验范围</strong><p>准入、事件影响、整改与申诉均需证据与人工审核。业务数据和处置为模拟，真实模型效果待配置验证；取证建议不会直接执行下架或通知。</p></div>
  </main>
</template>
