import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 35000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('opsai_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('opsai_token')
      localStorage.removeItem('opsai_user')
      window.dispatchEvent(new Event('opsai-unauthorized'))
    }
    return Promise.reject(err)
  }
)

export default api
