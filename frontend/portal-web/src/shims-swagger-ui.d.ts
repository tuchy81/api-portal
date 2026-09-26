// swagger-ui-dist v5 ships no types (@types/swagger-ui-dist only covers v3,
// whose API shape differs), so declare just the bits ApiDetail.vue uses.
declare module 'swagger-ui-dist' {
  export interface SwaggerUIBundleOptions {
    spec?: object
    url?: string
    domNode?: Element | null
    presets?: unknown[]
    plugins?: unknown[]
    layout?: string
    [key: string]: unknown
  }
  export interface SwaggerUIBundleFn {
    (options: SwaggerUIBundleOptions): unknown
    presets: { apis: unknown; base: unknown }
    plugins: Record<string, unknown>
  }
  export const SwaggerUIBundle: SwaggerUIBundleFn
  export const SwaggerUIStandalonePreset: unknown
}

declare module 'swagger-ui-dist/swagger-ui.css'
