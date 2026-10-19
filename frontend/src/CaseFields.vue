<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import {api,reportError} from './api'
type Product={id:string;merchant_id:string;brand:string;model:string;is_demo:boolean}
type Event={id:string;title:string;event_type:string;is_demo:boolean}
const props=defineProps<{workflow:string;merchantId:string;modelValue:Record<string,unknown>}>()
const emit=defineEmits<{ 'update:modelValue':[Record<string,unknown>] }>()
const products=ref<Product[]>([]),events=ref<Event[]>([]),loading=ref(false)
let epoch=0
onUnmounted(()=>{epoch++})
async function load(){
  const current=++epoch;loading.value=true
  try{
    const [items,incidents]=await Promise.all([api<Product[]>(`/products?limit=200&merchant_id=${encodeURIComponent(props.merchantId)}`),api<Event[]>('/events')])
    if(current===epoch){products.value=items;events.value=incidents}
  }catch(cause){if(current===epoch)reportError(cause,'业务资料读取失败')}
  finally{if(current===epoch)loading.value=false}
}
onMounted(load)
watch(()=>props.merchantId,()=>{products.value=[];update('sku_id','');load()})
const sku=computed(()=>typeof props.modelValue.sku_id==='string'?props.modelValue.sku_id:'')
const eventId=computed(()=>typeof props.modelValue.event_id==='string'?props.modelValue.event_id:'')
const asOf=computed(()=>typeof props.modelValue.as_of==='string'?props.modelValue.as_of:'')
function stringField(key:string){return typeof props.modelValue[key]==='string'?props.modelValue[key] as string:''}
function update(key:string,value:string){const payload={...props.modelValue};if(value)payload[key]=value;else delete payload[key];emit('update:modelValue',payload)}
function change(key:string,event:EventTarget|null){update(key,(event as HTMLInputElement).value)}
</script>
<template>
  <div class="form-grid business-fields">
    <label>资料适用日期<input type="date" :value="asOf" required @input="change('as_of',$event.target)"></label>
    <template v-if="workflow==='qualification'||workflow==='incident'">
      <label>{{workflow==='qualification'?'准入商品':'影响商品范围'}}<select :value="sku" :required="workflow==='qualification'" :disabled="loading" @change="change('sku_id',$event.target)"><option value="">{{workflow==='qualification'?'选择本商家的商品':'本商家全部商品'}}</option><option v-for="product in products" :key="product.id" :value="product.id">{{product.id}} · {{product.brand}} {{product.model}}</option><option v-if="sku && !products.some(p=>p.id===sku)" :value="sku" disabled>当前商品不可见，请重新选择</option></select></label>
      <label v-if="workflow==='incident'">已确认质量事件<select :value="eventId" required :disabled="loading" @change="change('event_id',$event.target)"><option value="">选择已确认事件</option><option v-for="row in events" :key="row.id" :value="row.id">{{row.is_demo?'模拟':'公开来源'}} · {{row.event_type==='recall'?'召回':row.event_type==='inspection'?'抽检':'未知'}} · {{row.title}}</option></select></label>
      <label v-if="workflow==='qualification'">声明型号（可选）<input :value="stringField('model')" maxlength="100" @input="change('model',$event.target)" placeholder="以原材料和目录核对"></label>
    </template>
    <template v-else-if="workflow==='remediation'">
      <label>复核类型<select :value="stringField('review_kind')" @change="change('review_kind',$event.target)"><option value="remediation">整改复核</option><option value="appeal">误判申诉</option></select></label>
      <label class="full-width">整改说明或申诉理由<textarea :value="stringField('statement')" required maxlength="3000" rows="4" @input="change('statement',$event.target)"></textarea></label>
    </template>
    <button type="button" class="text-button" :disabled="loading" @click="load">刷新商品与事件选项</button>
    <p v-if="loading" class="muted" role="status">正在读取可用商品和事件…</p>
    <p v-else-if="!products.length && workflow!=='remediation'" class="muted">该商家暂无可选商品，请由管理员检查商品目录。</p>
    <p v-else-if="products.length===200" class="muted">当前展示该商家前200件商品。</p>
  </div>
</template>
