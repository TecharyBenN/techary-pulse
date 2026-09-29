from pulse.adapters.gateway import gateway_model


def test_model_uses_the_gateway_and_configured_name() -> None:
    model = gateway_model("http://gateway.test:3000/v1", "key", "gateway-mid-model")

    assert model.model_name == "gateway-mid-model"
    assert model.base_url == "http://gateway.test:3000/v1/"
    assert model.settings is None
