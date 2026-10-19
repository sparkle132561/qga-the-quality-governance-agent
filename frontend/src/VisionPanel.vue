<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import {actor,api,imageObjectUrl,reportError} from './api'
type ReadingField={name:string;value:string|null;quote:string;box:number[]|null;certainty:string}
export type Vision={id:string;version?:string;status:string;engine_mode:string;model:string;source_sha256:string;reading:{image_quality:string;fields:ReadingField[];warnings:string[]};field_locations?:{status:string;reason?:string;ocr_text?:string}[];model_reading?:unknown;accepted_fields?:number[];corrected_regions?:{field_index:number;box:number[]}[];rejected_fields?:{index:number;proposal:unknown}[]}
const props=defineProps<{caseId:string;materialId:string;revision:number;status:string;vision?:Vision}>()
const emit=defineEmits<{updated:[]}>()
const open=ref(false),busy=ref(false),url=ref(''),selected=ref<number[]>([]),note=ref(''),focus=ref<number|null>(null)
const corrections=ref<Record<number,number[]>>({}),editing=ref(false)
let dragStart:number[]|null=null
let epoch=0
const editable=computed(()=>['draft','needs_info'].includes(props.status))
function savedRegion(index:number){return props.vision?.corrected_regions?.find(region=>region.field_index===index)?.box}
const boxStyle=computed(()=>{const box=focus.value===null?null:corrections.value[focus.value]??savedRegion(focus.value)??props.vision?.reading.fields[focus.value]?.box;return box?{left:`${box[0]/10}%`,top:`${box[1]/10}%`,width:`${(box[2]-box[0])/10}%`,height:`${(box[3]-box[1])/10}%`}:{display:'none'}})
function invalidate(){epoch++;busy.value=false;selected.value=[];note.value='';focus.value=null;corrections.value={};editing.value=false;dragStart=null;if(url.value)URL.revokeObjectURL(url.value);url.value=''}
function point(event:PointerEvent){const img=(event.currentTarget as HTMLElement).querySelector('img');if(!img)return null;const rect=img.getBoundingClientRect();return [Math.round(Math.max(0,Math.min(1000,(event.clientX-rect.left)/rect.width*1000))),Math.round(Math.max(0,Math.min(1000,(event.clientY-rect.top)/rect.height*1000)))]}
function startRegion(index:number){focus.value=index;editing.value=true;dragStart=null}
function pointerDown(event:PointerEvent){if(!editing.value||focus.value===null)return;event.preventDefault();dragStart=point(event);(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)}
function pointerMove(event:PointerEvent){if(!dragStart||focus.value===null)return;const end=point(event);if(end)corrections.value[focus.value]=[Math.min(dragStart[0]!,end[0]!),Math.min(dragStart[1]!,end[1]!),Math.max(dragStart[0]!,end[0]!),Math.max(dragStart[1]!,end[1]!)]}
function pointerUp(event:PointerEvent){if(!dragStart||focus.value===null)return;pointerMove(event);const box=corrections.value[focus.value];if(box&&(box[0]===box[2]||box[1]===box[3]))delete corrections.value[focus.value];dragStart=null;editing.value=false}
watch(()=>`${props.caseId}:${props.materialId}:${props.revision}:${props.status}:${props.vision?.id||''}`,()=>{invalidate();if(open.value)preview()})
onUnmounted(invalidate)
async function preview(){
 const current=epoch
 try{const value=await imageObjectUrl(`/cases/${props.caseId}/materials/${props.materialId}/vision-image`);if(current===epoch){if(url.value)URL.revokeObjectURL(url.value);url.value=value}else URL.revokeObjectURL(value)}catch(cause){if(current===epoch)reportError(cause,'图片预览失败')}
}
async function command(confirm=false){
 const current=++epoch;busy.value=true
 try{const body=confirm?{expected_revision:props.revision,candidate_id:props.vision?.id,accepted_fields:selected.value,note:note.value,corrected_regions:Object.entries(corrections.value).filter(([index])=>selected.value.includes(Number(index))).map(([index,box])=>({field_index:Number(index),box}))}:{expected_revision:props.revision};await api(`/cases/${props.caseId}/materials/${props.materialId}/vision${confirm?'/confirm':''}`,{method:'POST',body:JSON.stringify(body)});if(current===epoch)emit('updated')}
 catch(cause){if(current===epoch)reportError(cause,confirm?'视觉核验失败':'视觉读取失败')}
 finally{if(current===epoch)busy.value=false}
}
function toggle(event:Event){open.value=(event.target as HTMLDetailsElement).open;if(open.value&&!url.value)preview()}
</script>
<template><details class="vision-panel" @toggle="toggle"><summary>图片视觉核对（需Agnes，原图≤12MB）</summary><p class="muted">仅提取可见文字，不判断认证或内部质量。文字位置采用同一规范化图片的OCR对齐，仍需人工核对；未对齐不能直接写为证据。扫描PDF在上传时进行整页OCR，此处核对PNG/JPEG图片。</p><button v-if="editable&&actor?.role!=='admin'&&vision?.status!=='confirmed'" type="button" class="secondary" :disabled="busy" @click="command()">{{busy?'处理中…':'运行视觉读取'}}</button><button v-if="!url" type="button" class="text-button" @click="preview">读取私有图片预览</button>
<p v-if="editing" class="notice">请在图片上拖动框住该字段的完整引文；填写核验说明后才能提交。</p>
<div v-if="url" class="vision-preview" :style="editing?{cursor:'crosshair',touchAction:'none'}:{}" @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="dragStart=null;editing=false"><img :src="url" :draggable="false" alt="权限校验后的规范化材料图片"><div class="vision-region" :style="boxStyle" aria-label="视觉建议区域"></div></div>
<template v-if="vision"><p>视觉{{vision.status==='confirmed'?'所选字段已核验':'候选待核验'}} · {{vision.engine_mode}} · {{vision.model}} · 图片质量 {{vision.reading.image_quality}}</p><p v-for="(warning,index) in vision.reading.warnings" :key="index" class="notice">{{warning}}</p><details v-if="vision.rejected_fields?.length"><summary>未采用的读取（{{vision.rejected_fields.length}}）</summary><p class="notice">这些读取没有进入证据，仍需核对原图或补充资料。</p><p v-for="item in vision.rejected_fields" :key="item.index" class="source-unit">原读取{{item.index+1}}：{{JSON.stringify(item.proposal)}}</p></details>
<details v-if="vision.model_reading"><summary>查看原模型提议（坐标未作为定位依据）</summary><pre class="event-json">{{JSON.stringify(vision.model_reading,null,2)}}</pre></details>
<p v-if="vision.version!=='vision-v3'" class="notice">旧候选仅有模型建议框；请重新读取，或由审核员手动框选后确认。</p>
<article v-for="(field,index) in vision.reading.fields" :key="index" class="rule-card"><strong>{{field.name}}：{{field.value||'无法确定'}}</strong><p>{{field.quote}}</p><small>{{field.certainty==='clear'?'候选清晰读取':'不确定，不可作为证据'}}</small><p>{{corrections[index]?'已人工框选，待提交':savedRegion(index)?'人工修正区域已核验':vision.field_locations?.[index]?.status==='ocr_exact'?'OCR文字精确对齐，待人工核对':vision.field_locations?.[index]?.status==='ocr_approximate'?'OCR近似对齐，请核对识别差异':'待人工定位，不能直接采用模型框'}}</p><button v-if="field.box||corrections[index]||savedRegion(index)" type="button" class="text-button" @click="focus=index;editing=false">查看建议区域</button><button v-if="actor?.role==='reviewer'&&editable&&vision.status==='candidate'&&field.certainty==='clear'" type="button" class="text-button" @click="startRegion(index)">手动框选区域</button><label v-if="actor?.role==='reviewer'&&editable&&vision.status==='candidate'"><input v-model="selected" type="checkbox" :value="index" :disabled="field.certainty!=='clear'||['blurred','unreadable'].includes(vision.reading.image_quality)||(!corrections[index]&&(!field.box||vision.version!=='vision-v3'))">核验采用此字段</label></article>
<form v-if="actor?.role==='reviewer'&&editable&&vision.status==='candidate'" @submit.prevent="command(true)"><label>视觉字段核验说明<textarea v-model="note" required maxlength="500"></textarea></label><button class="primary" :disabled="busy||!selected.length||!note.trim()">确认所选视觉字段</button></form><p v-if="vision.status==='confirmed'" class="notice">已核验的是图片读取，不能据此证明认证有效或实物质量合格。</p></template></details></template>
