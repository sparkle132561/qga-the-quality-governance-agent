<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { actor, api, error, reportError } from './api'
import SearchPanel from './SearchPanel.vue'
import GraphPanel from './GraphPanel.vue'
type Source={id:string;title:string;url:string|null;source_kind:string;publisher:string;license_status:string;redistribution_allowed:boolean;published_on:string|null;verified_on:string}
type Rule={id:string;rule_key:string;version:string;source_id:string;effective_from:string;effective_until:string|null;content:string;is_demo:boolean;requirements:{documents?:string[]}}
type Product={id:string;merchant_id:string;brand:string;model:string;is_demo:boolean;batches:{id:string;number:string;production_date:string|null}[]}
type Event={id:string;title:string;event_type:string;source_id:string;is_demo:boolean;scope:Record<string,unknown>}
const sources=ref<Source[]>([]),rules=ref<Rule[]>([]),products=ref<Product[]>([]),events=ref<Event[]>([])
const asOf=ref('2025-07-01'),busy=ref(false)
async function load(){
  busy.value=true;error.value=''
  try{
    const result=await Promise.all([api<Source[]>('/sources'),api<Rule[]>(`/rules?as_of=${asOf.value}`),api<Product[]>('/products'),api<Event[]>('/events')])
    ;[sources.value,rules.value,products.value,events.value]=result
  }catch(cause){reportError(cause,'目录读取失败')}finally{busy.value=false}
}
async function importDemo(){
  busy.value=true
  try{await api('/admin/import-demo',{method:'POST'});await load()}
  catch(cause){reportError(cause,'导入失败')}finally{busy.value=false}
}
async function indexRules(){
  busy.value=true;error.value=''
  try{
    const result=await api<{added_chunks:number;missing_sources:string[]}>('/admin/index-rules',{method:'POST'})
    if(result.missing_sources.length)error.value='规则原文未缓存：'+result.missing_sources.join('、')+'。运行fetch-rule-source脚本后重新索引。'
  }catch(cause){reportError(cause,'索引失败')}finally{busy.value=false}
}
onMounted(load)
</script>
<template>
<main class="page"><section class="page-heading"><div><p class="eyebrow">VERSIONED SOURCES</p><h1>规则与来源</h1><p class="muted">按日期查看已收录规则，区分公开依据与项目自建演示资料。</p></div><button v-if="actor?.role==='admin'" class="primary" :disabled="busy" @click="importDemo">导入演示目录</button></section>
<div class="notice"><strong>来源与演示边界</strong><p>来源登记保留最小字段与链接；官方原文仅用于本地私有检索，不打包发布。模拟平台规则和商品目录不代表监管要求或真实企业资料。</p></div>
<div v-if="actor?.role==='admin'" class="action-bar catalog-panel"><button class="secondary" :disabled="busy" @click="indexRules">构建规则文本索引</button><span class="muted">官方原文保存在本地忽略目录，缺少缓存会明确提示。</span></div>
<SearchPanel />
<GraphPanel />
<section class="panel catalog-panel"><div class="panel-heading"><h2>适用规则版本</h2><form class="date-query" @submit.prevent="load"><label>查询日期<input v-model="asOf" type="date" required></label><button class="secondary" :disabled="busy">查询</button></form></div><p v-if="busy" class="muted" role="status">正在读取目录…</p><p v-else-if="!rules.length" class="empty small">该日期没有已收录的适用规则。管理员可先导入演示目录。</p><article v-for="rule in rules" :key="rule.id" class="rule-card"><div class="panel-heading"><strong>{{rule.rule_key}} · v{{rule.version}}</strong><span class="badge">{{rule.is_demo?'模拟平台规则':'官方规则参考'}}</span></div><p class="muted">{{rule.effective_from}} 起生效<template v-if="rule.effective_until">，{{rule.effective_until}} 起不再适用</template></p><p class="rule-content">{{rule.content}}</p><p v-if="rule.requirements.documents" class="muted">模拟必需资料：{{rule.requirements.documents.join('、')}}</p><small class="source-location">来源 {{rule.source_id}}</small></article></section>
<section class="panel"><h2>来源记录</h2><div class="table-wrap"><table><thead><tr><th>来源</th><th>类型</th><th>发布 / 核查</th><th>再分发</th></tr></thead><tbody><tr v-for="source in sources" :key="source.id"><td><a v-if="source.url" :href="source.url" target="_blank" rel="noopener noreferrer" class="detail-link">{{source.title}} ↗</a><strong v-else>{{source.title}}</strong><small>{{source.publisher}}</small></td><td>{{source.source_kind}}</td><td>{{source.published_on||'未记录'}}<small>核查 {{source.verified_on}}</small></td><td>{{source.redistribution_allowed?'项目自建资料':'许可未核实；仅链接与必要字段'}}</td></tr></tbody></table></div></section>
<section class="panel"><h2>模拟商品与批次</h2><div class="table-wrap"><table><thead><tr><th>SKU</th><th>商家</th><th>品牌 / 型号</th><th>批次 / 生产日</th></tr></thead><tbody><tr v-for="product in products" :key="product.id"><td>{{product.id}}<small>模拟</small></td><td>{{product.merchant_id}}</td><td>{{product.brand}}<small>{{product.model}}</small></td><td><div v-for="batch in product.batches" :key="batch.id">{{batch.number}}<small>{{batch.production_date||'未知'}}</small></div></td></tr></tbody></table></div></section>
<section class="panel"><h2>质量事件来源</h2><article v-for="event in events" :key="event.id" class="rule-card"><div class="panel-heading"><strong>{{event.title}}</strong><span class="badge">{{event.is_demo?'模拟事件':'官方历史事件'}}</span></div><p class="muted">{{event.event_type}} · {{event.source_id}}</p><details><summary>查看已收录范围字段</summary><pre class="event-json">{{JSON.stringify(event.scope,null,2)}}</pre></details></article></section>
</main>
</template>
