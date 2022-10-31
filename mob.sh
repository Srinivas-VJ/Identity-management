# start von
cd von/
./manage build
./manage start
cd ..

# start tail server
cd indy-tails-server/docker/
./manage start

cd ../../aries/demo

# start alice 
#TAILS_NETWORK=docker_tails-server ./run_demo alice --aip 10 --revocation --events 

# start faber
ngrok http 8020 > /dev/null &
TAILS_NETWORK=docker_tails-server ./run_demo faber --aip 10 --revocation --events 


