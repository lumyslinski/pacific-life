// Vite exposes build-time configuration on import.meta.env.
interface ImportMetaEnv {
    /** Origin + base path of the GEA API, e.g. https://api.example.com/gea/v1 or /api/gea/v1 behind a proxy. */
    readonly VITE_GEA_API_BASE_URL?: string;
}

interface ImportMeta {
    readonly env: ImportMetaEnv;
}
