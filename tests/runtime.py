from servicedesk_agent.api.app import create_app

app = create_app()
state = app.state.container
