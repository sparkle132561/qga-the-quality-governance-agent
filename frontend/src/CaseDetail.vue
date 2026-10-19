<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { actor, api, ApiError, error, reportError, workflowLabels, statusLabels, type Case, type Audit } from './api'
import MaterialPanel from './MaterialPanel.vue'
import SearchPanel from './SearchPanel.vue'
import AssistantPanel from './AssistantPanel.vue'
import IntentPanel from './IntentPanel.vue'
import CaseSummaryPanel from './CaseSummaryPanel.vue'
import GraphPanel from './GraphPanel.vue'
import PayloadEditor from './PayloadEditor.vue'
import ImpactPanel from './ImpactPanel.vue'
import ApprovalPanel from './ApprovalPanel.vue'
import MemoryPanel from './MemoryPanel.vue'
const route = useRoute()
const caseId = String(route.params.id)
let active = true
onUnmounted(() => { active = false })
const item = ref<Case | null>(null)
const events = ref<Audit[]>([])
const busy = ref(false)
const activeAction = ref('')
const cancelling = ref(false)
const cancellationNotice = ref('')
const loading = ref(true),evidenceOpen=ref(route.hash==='#intent-evidence')
function toggleEvidence(event:Event){evidenceOpen.value=(event.target as HTMLDetailsElement).open}
async function load() {
  try {
    item.value = await api<Case>(`/cases/${caseId}`)
    events.value = await api<Audit[]>(`/cases/${caseId}/audit`)
  } catch (cause) { if (active) reportError(cause, '读取失败') }
  finally {
    loading.value = false
    if(active&&route.hash.startsWith('#intent-')){
      await nextTick();document.getElementById(route.hash.slice(1))?.scrollIntoView()
    }
  }
}
onMounted(load)
async function command(action: string) {
  busy.value = true; activeAction.value=action; error.value = ''; cancellationNotice.value=''
  try { await api(`/cases/${caseId}/${action}`, { method: 'POST' }) }
  catch (cause) { if(!active)return; if(cause instanceof ApiError && cause.detail && typeof cause.detail==='object' && 'code' in cause.detail && cause.detail.code==='analysis_cancelled') cancellationNotice.value='取消已生效，案件可重新运行。'; else reportError(cause, '操作失败') }
  finally { busy.value = false; activeAction.value=''; if(active)await load() }
}
async function cancelAnalysis() {
  cancelling.value=true;error.value=''
  try {
    const status=await api<{revision:number;run_id:string|null}>(`/cases/${caseId}/analysis-status`)
    if(!status.run_id)throw new Error('当前没有可取消的运行，请刷新案件。')
    await api(`/cases/${caseId}/cancel-analysis`,{method:'POST',body:JSON.stringify({expected_revision:status.revision,analysis_run_id:status.run_id,note:'审核员从案件页面取消本次专业取证'})})
    cancellationNotice.value='已请求取消；正在等待已发出的调用结束，后续步骤与结果写回将停止。'
  }catch(cause){reportError(cause,'取消失败')}finally{cancelling.value=false}
}
const fieldLabels:Record<string,string>={as_of:'资料适用日期',sku_id:'商品编号',event_id:'质量事件',model:'声明型号',brand:'声明品牌',work_order_id:'关联工单',work_order_revision:'工单复核版本',review_kind:'复核类型',statement:'整改说明或申诉理由'}
const missingLabels:Record<string,string>={as_of:'补充适用日期',owned_sku_id:'选择本商家商品',event_id:'选择已确认事件',source_materials:'上传来源材料',new_review_evidence:'上传新的复核证据',candidate_scope_unknown:'补齐未知的事件范围或批次信息',product_evidence_coverage:'商品与批次来源尚未核验齐全',unverified_material_classification:'材料类型仍待核验',history_evidence_review:'相关历史仍待核验',applicable_rules:'缺少适用规则',applicable_remediation_rules:'缺少适用整改规则',review_evidence_basis:'新的复核证据尚未核验',review_statement:'补充复核说明'}
const documentLabels:Record<string,string>={certificate:'证书',label:'型号标签',test_report:'检测报告',model:'型号',power:'功率',certificate_number:'证书编号'}
function missingLabel(value:string){const [prefix,suffix]=value.split(':');if(prefix==='branch_review')return `${branchLabels[suffix]||suffix}待完成`;if(prefix==='material_field')return `材料缺少${documentLabels[suffix]||suffix}`;if(prefix==='document')return `缺少${documentLabels[suffix]||suffix}`;if(prefix==='material_parse')return '有材料尚未成功解析';if(value.includes('capacity')||value.includes('truncated'))return '资料范围超过本次检查上限';return missingLabels[value]||'仍有核验缺口（见来源与详细记录）'}
const branchLabels: Record<string,string> = { rules:'规则证据', product:'商品与订单匹配', material:'材料核对',history:'同商家历史' }
</script>

<template>
  <main class="page">
    <router-link to="/" class="back-link">← 返回案件列表</router-link>
    <p v-if="loading" role="status">正在读取案件…</p>
    <section v-else-if="item">
      <div class="page-heading"><div><p class="eyebrow">{{ workflowLabels[item.workflow] }} / DEMO</p><h1>{{ item.title }}</h1><p class="muted">{{ item.merchant_id }} · 资料版本 v{{ item.revision }}</p></div><span class="status" :class="item.status">{{ statusLabels[item.status] }}</span></div>
      <div class="action-bar"><button v-if="actor?.role !== 'admin' && ['draft','needs_info'].includes(item.status)" class="primary" :disabled="busy" @click="command('submit')">{{ busy ? '处理中…' : '提交案件' }}</button><button v-if="actor?.role === 'reviewer' && item.status === 'submitted'" class="primary" :disabled="busy" @click="command('analyze')">{{ busy ? '分支运行中…' : item.workflow==='qualification'?'运行准入取证':item.workflow==='incident'?'运行影响分析':item.payload.work_order_id?'运行整改／申诉复核':'运行骨架分析' }}</button><button v-if="actor?.role==='reviewer' && (activeAction==='analyze' || item.status==='analyzing')" class="secondary" :disabled="cancelling" @click="cancelAnalysis">取消当前取证</button><button v-if="actor?.role==='reviewer' && item.status==='analyzing' && !busy" class="secondary" @click="command('recover')">恢复中断运行</button><p class="muted">取证建议需人工决定；批准后只执行本地模拟处置。</p></div><p v-if="cancellationNotice" class="notice" role="status">{{cancellationNotice}}</p>
      <div class="detail-grid"><div><section class="panel"><h2>当前资料字段</h2><dl class="facts"><template v-for="(value,key) in item.payload" :key="key"><dt>{{ fieldLabels[key]||key }}</dt><dd>{{ typeof value === 'object' ? JSON.stringify(value) : value }}</dd></template></dl><p v-if="!Object.keys(item.payload).length" class="muted">暂无字段。</p></section>
        <PayloadEditor v-if="actor?.role!=='admin' && ['draft','needs_info'].includes(item.status)" :item="item" @saved="load" />
        <MaterialPanel id="intent-materials" :case-id="item.id" :status="item.status" :revision="item.revision" @updated="load" />
        <CaseSummaryPanel :item="item" />
        <MemoryPanel :item="item" />
        <ApprovalPanel id="intent-approval" :item="item" @updated="load" />
        <ImpactPanel v-if="item.latest_analysis?.impact" :impact="item.latest_analysis.impact" />
        <section id="intent-analysis" class="panel"><div class="panel-heading"><h2>并行分析</h2><span v-if="item.latest_analysis" class="badge">{{ item.latest_analysis.engine_mode.toUpperCase() }}</span></div><template v-if="item.latest_analysis"><p class="muted">本次运行 {{ (item.latest_analysis.elapsed_ms?.toFixed(1) ?? '未知') }} ms；{{item.latest_analysis.engine_mode==='stub'?'这是stub调度耗时。':'这是图取证阶段耗时；资料准备和写回另计，各分支记录实际调用模式。'}}</p><p v-if="item.latest_analysis.missing_fields?.length" class="alert">待补充：{{[...new Set(item.latest_analysis.missing_fields.map(missingLabel))].join('、')}}</p><details v-if="item.latest_analysis.conflicts?.length"><summary>查看字段冲突</summary><pre class="event-json">{{JSON.stringify(item.latest_analysis.conflicts,null,2)}}</pre></details><div v-for="(branch,key) in item.latest_analysis.branches" :key="key" class="branch"><span class="branch-symbol">↳</span><div><strong>{{ branchLabels[key] || key }}</strong><p>{{ branch.message }}</p><small>{{ branch.status }} · {{ branch.engine_mode||item.latest_analysis.engine_mode }} · {{ branch.evidence.length }} 条证据</small><small v-if="branch.error_code">{{branch.error_code}}</small><details v-if="branch.evidence.length"><summary>查看分支来源</summary><details v-for="source in branch.evidence" :key="source.id"><summary>{{source.id}} · {{JSON.stringify(source.location)}}</summary><p>{{source.text}}</p></details></details><details v-if="branch.trace?.length"><summary>查看专业取证步骤</summary><pre class="event-json">{{JSON.stringify(branch.trace,null,2)}}</pre></details></div></div><div class="alert">取证建议不是处置许可；审批与模拟处置见人工审核区。</div></template><p v-else class="empty small">尚未运行分析。提交后由审核员启动。</p></section>
        <details class="panel evidence-workspace" :open="evidenceOpen" @toggle="toggleEvidence"><summary>更多证据工具：检索、助手与关系</summary><template v-if="evidenceOpen"><IntentPanel :item="item"/><SearchPanel id="intent-evidence" :case-id="item.id"/><AssistantPanel :case-id="item.id"/><GraphPanel :case-id="item.id"/></template></details>
      </div><section class="panel timeline"><h2>操作记录</h2><ol><li v-for="event in events" :key="event.id"><strong>{{ event.action }}</strong><span>{{ event.actor_id }}</span><time>{{ new Date(event.created_at).toLocaleString('zh-CN') }}</time><details v-if="Object.keys(event.details).length"><summary>查看版本与操作详情</summary><pre class="event-json">{{JSON.stringify(event.details,null,2)}}</pre></details></li></ol></section></div>
    </section>
    <div v-else class="empty">无法读取此案件。请确认当前身份与案件归属。</div>
  </main>
</template>
