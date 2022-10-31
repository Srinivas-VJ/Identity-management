// testing endpoints for the api

async function getConnection() {
  const axios = require("axios");
  // post request to url of faber or any organization will return the connection_id which we can use to establish a connection
  const connection_response = await axios.post(
    "http://localhost:8021/connections/create-invitation"
  );
  // console.log("first response");
  // console.log(connection_response.data);
  // make alice accept the connection request
  const conn_details = await axios.post(
    "http://localhost:8031/connections/receive-invitation",
    connection_response.data.invitation
  );
  // console.log("second response");
  // console.log(conn_details.data);

  // accept the connection request (alice)
  conn_id = conn_details.data.connection_id;
  console.log(`connection id is ${conn_id}`);
  const accept_response = await axios.post(
    "http://localhost:8031/connections/" + conn_id + "/accept-invitation",
    {}
  );
  console.log("third response");
  console.log("alice accepted the connection request");
  console.log(accept_response.data);

  new_conn_id = "";
  const p = await axios.get("http://localhost:8021/connections");
  console.log(p.data.results);
  for (let i = 0; i < p.data.results.length; i++) {
    if (p.data.results[i].state == "request") {
      new_conn_id = p.data.results[i].connection_id;
    }
  }
  console.log(`new connection id is ${new_conn_id}`);

  // faber accepts the connection request
  const res = await axios.post(
    "http://localhost:8021/connections/" + new_conn_id + "/accept-request",
    {}
  );
  console.log("final response");
  console.log(res.data);

  // accept the connection request (faber)
}

// faber is doin this
getConnection();
