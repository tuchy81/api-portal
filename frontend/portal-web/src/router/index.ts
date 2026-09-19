import { createRouter, createWebHashHistory } from 'vue-router'
import CatalogList from '../views/CatalogList.vue'
import ApiDetail from '../views/ApiDetail.vue'
import ApplicationForm from '../views/ApplicationForm.vue'
import MyApplications from '../views/MyApplications.vue'
import ApprovalInbox from '../views/ApprovalInbox.vue'
import TokenList from '../views/TokenList.vue'
import UsageDashboard from '../views/UsageDashboard.vue'
import AuditLogTable from '../views/AuditLogTable.vue'

const routes = [
  { path: '/', redirect: '/cdp/catalog' },
  { path: '/cdp/catalog', component: CatalogList },
  { path: '/cdp/catalog/:apiId', component: ApiDetail },
  { path: '/cdp/apply/:apiId', component: ApplicationForm },
  { path: '/cdp/applications', component: MyApplications },
  { path: '/cdp/approvals', component: ApprovalInbox },
  { path: '/cdp/tokens', component: TokenList },
  { path: '/cdp/usage', component: UsageDashboard },
  { path: '/cdp/admin/audit', component: AuditLogTable },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
