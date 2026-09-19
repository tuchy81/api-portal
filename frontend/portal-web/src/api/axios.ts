import axios from 'axios'

const api = axios.create({
  baseURL: '/portal/v1',
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
})

// Auth token injection (in real setup, this comes from SSO session cookie)
// For dev, we use a localStorage mock token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('cdp_auth_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      window.location.hash = '/login'
    }
    return Promise.reject(err)
  }
)

export default api
