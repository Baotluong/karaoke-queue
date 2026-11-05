def scan_qr(client):
    """Do what a guest's phone does when scanning the party QR code."""
    code = client.app.state.store.join_code
    r = client.get(f"/join/{code}", follow_redirects=False)
    assert r.status_code == 303
    return client
