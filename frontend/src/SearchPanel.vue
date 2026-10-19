<script setup lang="ts">
import {ref,watch} from 'vue'
import {api,error,reportError} from './api'
const props=defineProps<{caseId?:string}>()
type Hit={id:string;text:string;location:Record<string,unknown>;document_id:string;rule_id:string|null;score:number;is_demo:boolean;source_sha256:string}
type Result={hits:Hit[];method?:string;timing_ms:{total:number;embedding?:number;end_to_end?:number};notice:string;route?:{route:string;reason:string;disabled:string[];clarification:string[]};paths?:unknown[];case_view?:unknown}
const query=ref('认证委托 注册证明'),asOf=ref('2025-07-01'),busy=ref(false),result=ref<Result|null>(null)
const method=ref('bm25')
const rewrite=ref(false),rerank=ref(false)
const policy=ref('manual')
let epoch=0
watch(()=>props.caseId,()=>{epoch++;result.value=null;busy.value=false})
async function search(){
  const current=++epoch,caseId=props.caseId
  busy.value=true;error.value='';result.value=null
  try{
    if(caseId)await api(`/cases/${caseId}/index-materials`,{method:'POST'})
    if(current!==epoch)return
    const path=caseId && policy.value!=='manual'?`/cases/${caseId}/routed-search`:'/search'
    const value=await api<Result>(path,{method:'POST',body:JSON.stringify(policy.value!=='manual' && caseId?{query:query.value,as_of:asOf.value,policy:policy.value,limit:5}:{query:query.value,as_of:asOf.value,case_id:caseId,method:method.value,limit:5,rewrite:rewrite.value,rerank:rerank.value})})
    if(current===epoch)result.value=value
  }catch(cause){if(current===epoch)reportError(cause,'检索失败')}finally{if(current===epoch)busy.value=false}
}
</script>
<template>
  <section class="panel">
    <h2>证据检索</h2>
    <form class="search-form" @submit.prevent="search">
      <label>检索问题<input v-model="query" required maxlength="2000" placeholder="输入需要核对的规则或材料"></label>
      <label v-if="caseId">取证策略<select v-model="policy"><option value="manual">手动选择增强</option><option value="adaptive">按问题自主择路</option><option value="all">全部可用增强（对照）</option></select></label>
      <div class="form-grid">
        <label>适用日期<input v-model="asOf" type="date" required></label>
        <label v-if="policy==='manual'">检索方法<select v-model="method"><option value="bm25">中文 BM25</option><option value="vector">向量（需配置Key并构建向量）</option><option value="hybrid">混合 RRF（需配置Key并构建向量）</option></select></label>
      </div>
      <div v-if="policy==='manual'" class="search-options"><label><input v-model="rewrite" type="checkbox"> 查询改写／拆解（需Agnes Key）</label><label><input v-model="rerank" type="checkbox"> 重排（需硅基流动Key）</label></div>
      <button class="secondary" :disabled="busy">{{busy?'检索中…':'检索证据'}}</button>
    </form>
    <template v-if="result">
      <p class="muted">{{result.route?.reason || result.method}} · {{(result.timing_ms.end_to_end ?? result.timing_ms.total).toFixed(1)}} ms · {{result.hits.length}} 个候选。结果是检索证据，不是审核结论。</p>
      <details v-if="result.route"><summary>取证策略与能力限制</summary><p>{{result.notice}}</p><pre>{{JSON.stringify(result.route,null,2)}}</pre></details>
      <details v-if="result.paths?.length"><summary>查看关系路径（{{result.paths.length}}）</summary><pre>{{JSON.stringify(result.paths,null,2)}}</pre></details>
      <details v-if="result.case_view"><summary>查看当前案件字段</summary><pre>{{JSON.stringify(result.case_view,null,2)}}</pre></details>
      <p v-if="!result.hits.length" class="empty small">没有匹配证据，请补充条件或资料。</p>
      <article v-for="hit in result.hits" :key="hit.id" class="rule-card">
        <div class="panel-heading"><strong>{{hit.document_id}}</strong><span class="badge">{{hit.is_demo?'模拟资料':'官方来源'}}</span></div>
        <details><summary>查看证据片段</summary><p class="evidence-text">{{hit.text}}</p></details>
        <small class="source-location">{{JSON.stringify(hit.location)}} · 分数 {{hit.score.toFixed(3)}}<br>快照 {{hit.source_sha256.slice(0,12)}}</small>
        <a v-if="typeof hit.location.source_url==='string'" :href="hit.location.source_url" class="detail-link" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a>
      </article>
    </template>
  </section>
</template>
<style scoped>pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
