<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { actor, api, downloadFile, error, reportError } from './api'
import VisionPanel, {type Vision} from './VisionPanel.vue'
const props = defineProps<{caseId:string;status:string;revision:number}>()
const emit = defineEmits<{updated:[]}>()
type Material = {id:string;filename:string;status:string;case_revision:number;parsed:{vision?:Vision;engine:string;warning:string|null;cleaning?:{changed_units:number;review_required:boolean;issues:{unit_index:number;code:string}[];units:{unit_index:number;text:string;operations:string[]}[]};units:{text:string;location:Record<string,unknown>}[];fields:{name:string;value:string;location:Record<string,unknown>;confidence:number|null;normalization?:{status:string;value?:string;unit?:string}}[]}}
const materials = ref<Material[]>([])
const selected = ref<File | null>(null)
const busy = ref(false)
const loading = ref(true)
let epoch=0
onUnmounted(()=>{epoch++})
async function load() {
  const current=++epoch
  try { const values=await api<Material[]>(`/cases/${props.caseId}/materials`);if(current===epoch)materials.value=values }
  catch (cause) { if(current===epoch)reportError(cause, '材料加载失败') }
  finally { if(current===epoch)loading.value = false }
}
watch(()=>`${props.caseId}:${props.revision}`,load,{immediate:true})
async function download(material:Material) {
  try { await downloadFile(`/cases/${props.caseId}/materials/${material.id}/download`, material.filename) }
  catch (cause) { reportError(cause, '下载失败') }
}
function choose(event:Event) { selected.value = (event.target as HTMLInputElement).files?.[0] || null }
async function upload() {
  if (!selected.value) return
  busy.value = true; error.value = ''
  const body = new FormData(); body.append('file', selected.value)
  try { await api(`/cases/${props.caseId}/materials`, {method:'POST',body}); selected.value=null; await load(); emit('updated') }
  catch (cause) { reportError(cause, '上传失败') }
  finally { busy.value=false }
}
const locationText = (location:Record<string,unknown>) => {
  if(location.kind==='pdf') return `PDF 第${location.page}页${location.method==='ocr' ? ' · 整页OCR' : ''}`
  if(location.kind==='sheet') return `${location.sheet} · ${location.cell}`
  if(location.kind==='csv') return `CSV 第${location.row}行、第${location.column}列`
  if(location.kind==='image') return `图片文字框 ${JSON.stringify(location.box)}`
  return `HTML 片段${location.fragment}`
}
</script>
<template>
  <section class="panel"><h2>材料与来源</h2>
    <form v-if="actor?.role !== 'admin' && ['draft','needs_info'].includes(status)" class="upload-form" @submit.prevent="upload"><label>上传资料（单文件不超过12MB）<input type="file" accept=".pdf,.html,.htm,.xlsx,.csv,.png,.jpg,.jpeg" :disabled="busy" @change="choose"></label><button class="secondary" :disabled="busy || !selected">{{ busy ? '解析中…' : '上传并解析' }}</button></form>
    <p v-if="loading" class="muted" role="status">正在读取材料…</p><p v-else-if="!materials.length" class="empty small">暂无材料。上传后可查看每个字段的原始位置。</p>
    <article v-for="material in materials" :key="material.id" class="material"><div class="panel-heading"><strong>{{ material.filename }}</strong><span class="badge">{{ material.status }}</span></div><p class="muted">资料版本 v{{ material.case_revision }} · {{ material.parsed.engine }}</p><button class="text-button" @click="download(material)">下载原始文件</button><p v-if="material.parsed.warning" class="alert">{{ material.parsed.warning }}</p><dl class="facts"><template v-for="(field,index) in material.parsed.fields" :key="index"><dt>{{ field.name }}</dt><dd>{{ field.value }}<small v-if="field.normalization" class="source-location">规范数量：{{ field.normalization.status==='normalized' ? `${field.normalization.value} ${field.normalization.unit}` : '存在歧义，待人工复核' }}</small><small class="source-location">{{ locationText(field.location) }}<template v-if="field.confidence !== null"> · OCR分数 {{ field.confidence.toFixed(2) }}</template></small></dd></template></dl><details v-if="material.parsed.cleaning"><summary>清洗记录：{{ material.parsed.cleaning.changed_units }}处文字规范化 · {{ material.parsed.cleaning.issues.length }}个复核提示</summary><p class="muted">原文保留；规范化仅处理字符形式与空白，不猜测错字或缺失值。OCR分数不代表正确概率。</p><div v-for="unit in material.parsed.cleaning.units.filter(item=>item.operations.length)" :key="unit.unit_index" class="source-unit"><p>原文：{{ material.parsed.units[unit.unit_index]?.text }}</p><p>规范化：{{ unit.text }}</p></div><p v-for="issue in material.parsed.cleaning.issues" :key="`${issue.unit_index}:${issue.code}`">片段{{ issue.unit_index + 1 }}：{{ issue.code==='low_ocr_score' ? 'OCR分数偏低，请核对原件' : '包含无法解码的字符，请核对原件' }}</p></details><details><summary>查看来源片段（{{ material.parsed.units.length }}）</summary><div v-for="(unit,index) in material.parsed.units" :key="index" class="source-unit"><small>{{ locationText(unit.location) }}</small><p>{{ unit.text }}</p></div></details><VisionPanel v-if="/\.(png|jpe?g)$/i.test(material.filename)" :case-id="caseId" :material-id="material.id" :revision="revision" :status="status" :vision="material.parsed.vision" @updated="emit('updated')" /></article>
  </section>
</template>
