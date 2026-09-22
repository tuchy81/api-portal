import { createRouter, createWebHashHistory } from 'vue-router'
import CatalogList from '../views/CatalogList.vue'
import ApiDetail from '../views/ApiDetail.vue'
import ApplicationForm from '../views/ApplicationForm.vue'
import MyApplications from '../views/MyApplications.vue'
import ApprovalInbox from '../views/ApprovalInbox.vue'
import ApiRegisterForm from '../views/ApiRegisterForm.vue'
import ApiEditForm from '../views/ApiEditForm.vue'
import TokenList from '../views/TokenList.vue'
import UsageDashboard from '../views/UsageDashboard.vue'
import AuditLogTable from '../views/AuditLogTable.vue'
import AdminTokenTable from '../views/AdminTokenTable.vue'
import Login from '../views/Login.vue'

const routes = [
  { path: '/login', component: Login, meta: { public: true } },
  { path: '/', redirect: '/cdp/catalog' },
  { path: '/cdp/catalog', component: CatalogList },
  { path: '/cdp/catalog/new', component: ApiRegisterForm },
  { path: '/cdp/catalog/:apiId/edit', component: ApiEditForm },
  { path: '/cdp/catalog/:apiId', component: ApiDetail },
  { path: '/cdp/apply/:apiId', component: ApplicationForm },
  { path: '/cdp/applications', component: MyApplications },
  { path: '/cdp/approvals', component: ApprovalInbox },
  { path: '/cdp/tokens', component: TokenList },
  { path: '/cdp/usage', component: UsageDashboard },
  { path: '/cdp/admin/tokens', component: AdminTokenTable },
  { path: '/cdp/admin/audit', component: AuditLogTable },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to) => {
  const hasToken = !!localStorage.getItem('cdp_auth_token')
  if (!to.meta.public && !hasToken) {
    return '/login'
  }
})

export default router
