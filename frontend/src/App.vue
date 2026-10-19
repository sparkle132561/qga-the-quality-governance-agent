<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { actor, error, logout, restore, roleLabels, signIn, reportError } from './api'
const router = useRouter()
const protocolDemo = import.meta.env.VITE_PROTOCOL_DEMO === '1'
const busy = ref(false)
const restoring = ref(true)
onMounted(async () => { await restore(); restoring.value = false })
async function enter(username: string) {
  busy.value = true; error.value = ''
  try { await signIn(username); await router.push('/') }
  catch (cause) { reportError(cause, '登录失败') }
  finally { busy.value = false }
}
</script>

<template>
  <div v-if="restoring" class="loading-screen" role="status">正在恢复工作台…</div>
  <main v-else-if="!actor" class="login-page">
    <section class="login-intro">
      <p class="eyebrow">QUALITY GOVERNANCE / LOCAL DEMO</p>
      <h1>让每一次审核，<br>都有证据可循。</h1>
      <p>把商品资料、质量事件与整改过程放在同一条可追溯的工作流中。</p>
      <div class="steps"><span>01 · 准入</span><span>02 · 影响分析</span><span>03 · 整改复核</span></div>
    </section>
    <section class="login-panel">
      <p class="eyebrow">进入工作台</p><h2>选择演示身份</h2>
      <p class="muted">本地演示账号；业务数据均为模拟。生产身份认证不在此入口提供。</p>
      <div v-if="error" class="alert error" role="alert">{{ error }}</div>
      <button v-for="user in [{id:'merchant-a',name:'商家甲',hint:'提交材料，查看自己的案件'},{id:'merchant-b',name:'商家乙',hint:'检验商家之间的数据隔离'},{id:'reviewer',name:'平台审核员',hint:'查看提交队列与分析证据'},{id:'admin',name:'管理员',hint:'查看全局记录与管理入口'}]" :key="user.id" :disabled="busy" class="identity" @click="enter(user.id)">
        <span><strong>{{ user.name }}</strong><small>{{ user.hint }}</small></span><span aria-hidden="true">↗</span>
      </button>
    </section>
  </main>
  <div v-else class="app-shell">
    <aside class="sidebar">
      <router-link class="brand" to="/"><span class="brand-icon">Q</span><span>商品质量治理<small>Evidence workspace</small></span></router-link>
      <p class="nav-heading">工作空间</p><router-link to="/" class="nav-link">案件工作台</router-link>
      <router-link to="/catalog" class="nav-link">规则与来源</router-link>
      <router-link v-if="actor.role!=='merchant'" to="/incidents" class="nav-link">质量事件导入</router-link>
      <router-link to="/work-orders" class="nav-link">整改与申诉工单</router-link>
      <router-link to="/memories" class="nav-link">记忆与核验</router-link>
      <div class="sidebar-bottom"><span class="dot"></span>本地演示环境<p>模拟数据 · 人工确认 · 来源追溯</p></div>
    </aside>
    <div class="main-shell">
      <header class="topbar"><span>{{ roleLabels[actor.role] }}工作台</span><div><span class="badge">{{ actor.user_id }}</span><button class="text-button" @click="logout(); error = ''">退出</button></div></header>
      <div v-if="error" class="alert error page-alert" role="alert">{{ error }}<button class="text-button" @click="error = ''">关闭</button></div>
      <p v-if="protocolDemo" class="notice demo-mode">协议演示：模型回应固定为 Fake，业务处置仅写本地记录；真实模型质量尚未验证。</p>
      <router-view :key="`${actor.user_id}:${$route.fullPath}`" />
    </div>
  </div>
</template>
