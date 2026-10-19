<script setup lang="ts">
import {ref,watch} from 'vue'
import {api,error,reportError,type Case} from './api'
import CaseFields from './CaseFields.vue'
const props=defineProps<{item:Case}>()
const emit=defineEmits<{saved:[]}>()
const payload=ref<Record<string,unknown>>({...props.item.payload}),busy=ref(false)
watch(()=>props.item.revision,()=>{payload.value={...props.item.payload}})
async function save(){
  busy.value=true;error.value=''
  try{
    await api(`/cases/${props.item.id}/payload`,{method:'PUT',body:JSON.stringify({expected_revision:props.item.revision,payload:payload.value})})
    emit('saved')
  }catch(cause){reportError(cause,'资料编辑失败')}finally{busy.value=false}
}
</script>
<template><details class="panel"><summary>补充或修改资料声明</summary><p class="muted">声明与原始材料分开保存；保存会产生新版本并清除旧分析。工单绑定由服务器维护。</p><form class="search-form" @submit.prevent="save"><CaseFields v-model="payload" :workflow="item.workflow" :merchant-id="item.merchant_id"/><button class="secondary" :disabled="busy">保存资料新版本</button></form></details></template>
