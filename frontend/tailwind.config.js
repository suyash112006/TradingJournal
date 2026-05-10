/** @type {import('tailwindcss').Config} */
module.exports = {
    content: [
        "./src/pages/**/*.html",
        "./src/assets/js/**/*.js",
        "./index.html"
    ],
    theme: {
        extend: {
            colors: {
                bg: {
                    DEFAULT: '#06090D',
                    sidebar: '#070B10',
                    card: '#0B0F14',
                    hover: '#0F1623'
                },
                card: {
                    DEFAULT: '#0B0F14',
                    top: '#111827'
                },
                primary: {
                    DEFAULT: '#22C55E',
                    dark: '#16A34A',
                    glow: 'rgba(34, 197, 94, 0.45)',
                    soft: 'rgba(34, 197, 94, 0.12)'
                },
                secondary: {
                    DEFAULT: '#A855F7',
                    soft: 'rgba(168, 85, 247, 0.15)'
                },
                icon: {
                    blue: '#38BDF8',
                    bg: '#0C1220'
                },
                text: {
                    DEFAULT: '#E5E7EB',
                    secondary: '#9CA3AF',
                    muted: '#6B7280',
                    disabled: '#4B5563'
                },
                danger: {
                    DEFAULT: '#EF4444',
                    soft: 'rgba(239, 68, 68, 0.15)'
                },
                green: '#22C55E',
                muted: '#9CA3AF',
            },
            backgroundImage: {
                'card-gradient': 'linear-gradient(180deg, #111827 0%, #0B0F14 100%)',
            },
            boxShadow: {
                'glow': '0 0 0 1px rgba(34, 197, 94, 0.4), 0 20px 40px rgba(0, 0, 0, 0.7)',
            }
        },
    },
    plugins: [],
}
