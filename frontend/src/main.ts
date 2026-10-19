import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import Workspace from './Workspace.vue'
import CaseDetail from './CaseDetail.vue'
import CatalogPage from './CatalogPage.vue'
import IncidentPage from './IncidentPage.vue'
import WorkOrdersPage from './WorkOrdersPage.vue'
import MemoryPage from './MemoryPage.vue'
import './style.css'

const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/', component: Workspace },
  { path: '/cases/:id', component: CaseDetail },
  { path: '/catalog', component: CatalogPage },
  { path: '/incidents', component: IncidentPage },
  { path: '/work-orders', component: WorkOrdersPage },
  { path: '/memories', component: MemoryPage },
] })
createApp(App).use(router).mount('#app')
