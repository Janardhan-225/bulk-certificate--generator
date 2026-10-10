from certgen import create_app

app = create_app()

if __name__ == "__main__":
    # threaded=True lets status polls be served while a job is generating.
    app.run(host="127.0.0.1", port=5000, threaded=True)
