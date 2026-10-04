# Atlas - deploy as a website (about 5 minutes, free)

1. Create a free account at github.com and a new empty repository.
2. Click "Add file > Upload files", drag in everything from this folder, commit.
3. Create a free account at render.com > New > Blueprint > pick your repository.
4. When asked for CONTACT_EMAIL, enter your email. Click Apply.
5. Render gives you a public https://atlas-maps-xxxx.onrender.com link. Done.

Note: the free plan sleeps after inactivity (first visit takes ~30s to wake).
For real traffic, change GEOCODE_URL / ROUTE_URL / TILE_URL in Render's
Environment tab to a hosted map provider (see .env.example).
