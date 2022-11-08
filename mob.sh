killall ngrok

# # start von
# cd von/
# ./manage build
# ./manage start

# echo "started von"

# sleep 3

# cd ..


# start tail server
cd indy-tails-server/docker/
./manage start

echo "started tail server"

cd ../../aries/demo


# start organisation
if [ "$1" = "faber" ]; then
  AGENT_PORT=8020

elif [ "$1" = "pes" ]; then
  AGENT_PORT=8100

elif [ "$1" = "alice" ]; then
  AGENT_PORT=8030

elif [ "$1" = "acme" ]; then
  AGENT_PORT=8040

elif [ "$1" = "performance" ]; then
  AGENT_PORT=8050

else
  echo "Please specify which agent you want to run. Choose from 'faber', 'pes', 'alice', 'acme', or 'performance'."
  exit 1
fi

ngrok http $AGENT_PORT > /dev/null &

echo "started ngrok"

sleep 3

TAILS_NETWORK=docker_tails-server LEDGER_URL=http://test.bcovrin.vonx.io ./run_demo "$1" --aip 10 --revocation --events 

# TAILS_NETWORK=docker_tails-server LEDGER_URL=http://prod.bcovrin.vonx.io ./run_demo "$1" --aip 10 --revocation --events 

