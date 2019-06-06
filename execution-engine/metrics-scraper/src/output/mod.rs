use std::net::SocketAddr;
use std::thread;
use std::thread::JoinHandle;

use hyper::rt::Future;
use hyper::{service, Body, Method, Request, Response, Server, StatusCode};

use metrics_scraper::accumulator::Drainer;

fn parse_line(item: String) -> Option<String> {
    const TIME_SERIES_DATA_KEY: &str = "time-series-data";
    let ret = {
        match shared::logging::logger::LogLineItem::from_log_line(&item) {
            Some(log_line_item) => {
                if let Some(metric) = log_line_item
                    .properties
                    .get(&TIME_SERIES_DATA_KEY.to_string())
                {
                    return Some(metric.to_string());
                }

                None
            }
            None => None,
        }
    };
    ret
}

fn handler_factory<D: Drainer<String>>(drainer: D) -> impl FnMut(Request<Body>) -> Response<Body> {
    move |req: Request<Body>| {
        let mut response = Response::new(Body::empty());
        match (req.method(), req.uri().path()) {
            (&Method::GET, "/") => match drainer.drain() {
                Ok(ret) => {
                    let parsed_lines: String = ret
                        .into_iter()
                        .filter_map(parse_line)
                        .collect::<Vec<String>>() //
                        .join("\n");
                    *response.body_mut() = Body::from(parsed_lines);
                }
                Err(err) => {
                    *response.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
                    *response.body_mut() = Body::from(err.to_string());
                }
            },
            _ => {
                *response.status_mut() = StatusCode::NOT_FOUND;
            }
        };

        response
    }
}

pub(crate) fn open_drain<D: Drainer<String> + 'static>(
    drainer: D,
    addr: &SocketAddr,
) -> JoinHandle<()> {
    let service = move || {
        let drainer = drainer.clone();
        service::service_fn_ok(handler_factory(drainer))
    };
    let server = Server::bind(addr)
        .serve(service)
        .map_err(|e| eprintln!("server error: {}", e));

    thread::spawn(move || hyper::rt::run(server))
}

#[cfg(test)]
mod tests {
    use std::string::ToString;

    use super::*;

    #[test]
    fn should_parse_metric_line() {
        let metric_line = r#"2019-06-05T22:24:35.878Z METRIC 6 system76-pc casperlabs-engine-grpc-server payload={"timestamp":"2019-06-05T22:24:35.878Z","process_id":6507,"process_name":"casperlabs-engine-grpc-server","host_name":"system76-pc","log_level":"Metric","priority":6,"message_type":"ee-structured","message_type_version":"1.0.0","message_id":"6682069017946818164","description":"trie_store_write_duration write 0.001382911","properties":{"correlation_id":"38b81cd8-b089-42c0-bdeb-2e3dc2a91255","duration_in_seconds":"0.001382911","message":"trie_store_write_duration write 0.001382911","message_template":"{message}","time-series-data":"trie_store_write_duration{tag=\"write\", correlation_id=\"38b81cd8-b089-42c0-bdeb-2e3dc2a91255\"} 0.001382911 1559773475878"}}"#;
        let parsed = parse_line(metric_line.to_string()).expect("foo");
        assert_eq!(parsed,r#"trie_store_write_duration{tag="write", correlation_id="38b81cd8-b089-42c0-bdeb-2e3dc2a91255"} 0.001382911 1559773475878"#, "should match literal input");
    }
}
