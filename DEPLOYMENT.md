# GitHub Pages Deployment

This repository is a static website. No build step is required.

## Local Run

```bash
cd ~/regional-incident-data-lake
python3 -m http.server 8088 --bind 127.0.0.1
```

Open:

```text
http://127.0.0.1:8088/
```

## Deploy to GitHub Pages

1. Push the repository to GitHub.
2. Open the repository on GitHub.
3. Go to **Settings -> Pages**.
4. Under **Build and deployment**, choose **Deploy from a branch**.
5. Choose branch **main** and folder **/root**.
6. Save.

GitHub will serve `index.html` as the homepage. Keep these folders in the repository:

- `deliverables/`
- `screenshots/`
- `plan/`
- `docs/`

The `.nojekyll` file is included so GitHub Pages serves all static files directly.
